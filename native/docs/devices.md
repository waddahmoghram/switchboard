# Native WLED and Shelly devices

These are direct C++20 adapters, not Python launchers. Both families use the native web transport,
bounded JavaScript Object Notation (JSON) and named, fixed configuration entries. Registration checks
`context.enabled("wled")` and `context.enabled("shelly")` independently. An unselected family's settings
are ignored. Registration, catalogs and inventory do not contact devices or resolve credentials.

**Verification boundary:** 69 synthetic cases are authored in [devices.cpp](../tests/devices.cpp), all
**UNRUN**. The permitted compiler syntax check completed without diagnostics. No project import,
application/example execution, test collection, test execution, configure/build, account access or
device probe was performed. Public provider documentation was retrieved on 2026-11-11. Syntax checking
does not establish linkage, running behavior, production transport authentication or firmware compatibility.

## Setup and configuration

Merge [devices.example.json](../config/devices.example.json) deliberately, preserving other selected
connectors, policies and exposed tools. Addresses and identity in it are synthetic. Replace them only
after inspecting your own devices. It starts **read-only**, including WLED: neither family registers
actions until the respective device's `allow_control` is true. Keep approvals enabled and inspect existing
per-tool overrides. `Tier::act` asks assistants and automation rules by default; operator policy can
change that behavior. These adapters do not bypass the dispatcher.

The root `switchboard`, `connectors`, `policy` and `mcp` objects follow the native configuration layout.
The native data directory and native core approval/audit storage must remain separate from Python state.
Do not point a native database at the Python database. These adapters create **no connector tables,
migrations, jobs, events, subscriptions or background network work**. They do not cache snapshots or
retain energy history. In particular, native WLED does not emit Python's `wled.action_result`.

### Fixed origins and transport

Hypertext Transfer Protocol (HTTP) requires explicit `allow_http: true` on each device. This is consent to
unencrypted local traffic, not authentication or proof of ownership. Keep devices on an isolated,
operator-owned network and never expose their control ports publicly. Encrypted endpoints use
Transport Layer Security (TLS) certificate verification through the shared native transport; there is
no certificate bypass or encrypted-to-plain fallback.

Only a root `http://` or `https://` origin is accepted. A trailing slash and default port are normalized.
Embedded credentials, other paths, query strings, fragments, escapes, whitespace and alternate schemes
are refused. Targets cannot be supplied in tool arguments. Duplicate normalized origins are refused
within a family; Shelly identities must also be unique.

- WLED accepts private and loopback Internet Protocol version 4 (IPv4) literals, IPv4 link-local
  literals, Internet Protocol version 6 (IPv6) unique-local and loopback literals, `localhost`, and
  hostnames ending in `.local` or `.home.arpa`. Hostnames are resolved only by the transport at use time.
- Shelly plain HTTP additionally requires a private numeric address in `10/8`, `172.16/12`,
  `192.168/16` or `fc00::/7`. Hostnames, loopback and link-local addresses are refused for plain HTTP.
  An encrypted configured origin may be an operator-managed trusted proxy with a valid hostname;
  it must preserve the device identity, protocol and authentication challenge.

Requests have response caps and timeouts and never request redirect following. The shared transport
must preserve response headers, particularly `WWW-Authenticate`; an empty-header transport fails Shelly
authentication closed. The adapter checks response sizes even with a fake transport. Transport and
provider exception text, addresses, headers and error bodies are never copied into returned errors.
Errors use fixed `devices_*` categories.

## Tools and approval bindings

All tools reject unknown properties. All live reads are on demand, without cached or empty-success
fallback. `READ` and `ACT` below denote literal policy tiers, not physical outcomes.

| Tool                  | Tier   | Required arguments                                          | Availability                                |
| --------------------- | ------ | ----------------------------------------------------------- | ------------------------------------------- |
| `wled_devices`        | `READ` | Empty object                                                | When WLED is selected                       |
| `wled_status`         | `READ` | `device`                                                    | When WLED is selected                       |
| `wled_set_power`      | `ACT`  | `device`, `target_digest`, boolean `on`; optional `segment` | Any opted-in WLED device                    |
| `wled_set_brightness` | `ACT`  | `device`, `target_digest`, `brightness`; optional `segment` | Any opted-in WLED device                    |
| `wled_set_color`      | `ACT`  | `device`, `target_digest`, `segment`, `color`               | Any opted-in WLED device                    |
| `wled_set_transition` | `ACT`  | `device`, `target_digest`, `transition_deciseconds`         | Any opted-in global scope                   |
| `wled_apply_preset`   | `ACT`  | `device`, `target_digest`, `preset`                         | Any configured preset on an opted-in device |
| `shelly_devices`      | `READ` | Empty object                                                | When Shelly is selected                     |
| `shelly_status`       | `READ` | `device`, integer `switch_id: 0`                            | When Shelly is selected                     |
| `shelly_switch_set`   | `ACT`  | `device`, integer `switch_id: 0`, `target`, boolean `on`    | Any completely opted-in Shelly control      |

Inventory returns aliases, scopes and safety limits, not addresses, secret references or live health.
For WLED, copy `target_digest` from the exact entry in `devices[].targets`:
`scope: "segment"` includes its integer `segment`; `scope: "global"` authorizes only the global selection.
Presets have separate fingerprints in `devices[].presets[].target_digest`. A segment binding is not
a global or preset binding. For Shelly, copy `target` from `devices[].controls[]` for the chosen component.
There are no action bindings on read-only devices. Status tools do not require action bindings.

Every action schema requires and enumerates current bindings; handlers independently check the binding
against the selected device and scope before any network request. The core must also revalidate stored
approval arguments at execution. Do not repair an old approval by silently substituting a new fingerprint.

Bindings use the native `hash(Json)` helper: the `SHA-256` algorithm over compact, sorted-key JSON with
non-seven-bit characters escaped and no trailing newline. Native engine tags are
`switchboard_cpp_wled_static_v1` and `switchboard_cpp_shelly_plug_v1`; these bindings are not interchangeable
with Python bindings. The complete input objects are defined by `wled_configuration`/`wled_binding` and
`shelly_binding` in [the implementation](../src/devices.cpp).

- WLED binds the normalized origin, alias, transport consent, control/global opt-ins, complete sorted
  segment and preset allowlists, all reviewed preset identifiers/fingerprints, brightness/transition caps,
  transport limits, fixed safety rules and selected segment/global/preset scope.
- Shelly binds the normalized origin, alias, expected identity/model/generation, component, transport
  consent, password **reference**, control/safety opt-ins, power cap, transport limits, timer restriction
  and authentication profile. Resolved credentials never enter a binding.

Changes require fresh inventory and approval after rebuilding the registry/restarting the process.
Running processes retain their original immutable configuration; configuration is not hot-reloaded.
Credential content rotation under an unchanged reference does not change the binding. Bindings do not
authenticate a physical device or freeze its state against other controllers.

## WLED protocol and safety

Configure 1-16 uniquely named devices, each with 1-16 distinct segment identifiers in 0-63 and at most
16 presets with distinct names and identifiers. Aliases start with a lowercase letter and contain lowercase letters,
digits or underscores, at most 32 characters. Timeout is 1-30 seconds, default 5; response cap is
4,096-262,144 bytes, default 65,536. Additional native settings are:

| Device setting               | Default | Meaning                                                              |
| ---------------------------- | ------- | -------------------------------------------------------------------- |
| `allow_control`              | `false` | Required for any action                                              |
| `allow_global`               | `false` | Required for whole-device power/brightness and persistent transition |
| `max_brightness`             | `128`   | Integer 1-255, limits requested and recalled brightness              |
| `max_transition_deciseconds` | `100`   | Integer 0-100; cannot permit more than ten seconds                   |

Read uses exactly `GET /json/si`, not configuration or effect catalogs. Returned segment details are
filtered to the configured identifiers; missing segments and unknown capabilities remain explicit.
Network identifiers and arbitrary device metadata are omitted. There is no WLED energy measurement.
The fixed protocol subset is based on the documented fields, not certification of every WLED version.
Missing required state fields reject reads. Older firmware without explicit segment identifiers,
`maxseg`, per-segment capabilities or overriding-mode reports cannot pass the relevant control preflight.

Power is an absolute boolean, never a toggle. Brightness is an integer 1-255 and cannot exceed the device
cap; zero is refused in favor of explicit power off. Brightness/color writes do not add power-on commands.
Color is three or four byte channels, for red/green/blue and optional white, and sets `fx: 0` (Solid).
Reported per-segment capabilities are indexed by segment identifier. Unreported color support and
unsupported white channels fail closed; white-only and color-temperature controls are not implemented.

Global operations affect **all** device segments, not only the configured segment list. Global power-on
refuses unreported/above-cap active segment brightness. Segment power-on, color, effect and brightness
updates also refuse recalled global/target-segment brightness above the cap. A transition uses the
readable persistent `transition` field: one decisecond is 100 milliseconds; `4` means 400 milliseconds.
The unobservable one-call `tt` field is not used.

Every action reads a fresh preflight snapshot and refuses inactive/missing target segments, unsupported
controls, missing bounds/capabilities, or active **or unreported** realtime, playlist or nightlight modes.
The one state write is `POST /json/state`, adding `{"udpn":{"nn":true}}` to suppress the documented one-call
datagram synchronization notification. This does not disable independently configured synchronization,
user extensions, other controllers or wired side effects.

### Reviewed static presets

WLED presets can contain general commands and playlists. A configured name/id alone never authorizes
execution. Each reviewed preset configuration contains `name`, integer `id` in 1-250 and lowercase
64-character `sha256` for the **entire** selected object.

The adapter fetches bounded `GET /presets.json`, validates the selected object, verifies its fingerprint,
and copies its static state into the ordinary write. It never posts `ps`, executes the firmware preset,
or follows nested presets. Metadata is fingerprinted but stripped from the write. A subsequent file edit
cannot replace the already copied patch.

Allowed top-level fields are `n`, `ql`, boolean `on`, bounded `bri`, bounded `transition` and `seg`.
`n`/`ql` are printable seven-bit strings of at most 128/16 characters. Segment entries require explicit
integer `id` plus at least one of boolean `on`, bounded `bri`, `col` (one to three byte-channel colors),
or integer `fx: 0`. Color-bearing entries must explicitly include `fx: 0`. Duplicate/unconfigured segments,
geometry, saving/deleting presets, relative/random/toggle values, macros and every unknown field are
refused even with a matching fingerprint. Metadata-only objects are refused.

For example, this is a selected static object, not a runnable command or complete configuration:

```json
{"n":"Warm desk","seg":[{"id":0,"on":false,"bri":64,"col":[[80,40,8]],"fx":0}]}
```

Manually review the complete object and compute its canonical fingerprint with a trusted offline tool
using the serialization above. Never automatically refresh it after a mismatch. Full state presets saved
by ordinary device interfaces often contain unsupported fields and are intentionally rejected rather
than partially applied. A segment-only preset can operate with global control disabled.

## Shelly compatibility, authentication and load review

Native reads **and** controls support only generation 2 Shelly Plus Plug S (`SNPL-00112EU`) and Shelly
Plus Plug US (`SNPL-00116US`), and only `switch:0`. Unsupported models, generations and component lists
fail configuration validation; there is no generic-model fallback. Official device pages identify the
components and note feature freeze at firmware `1.7.x`, not tested firmware compatibility.

Configure 1-16 named devices. `expected_device_id`, `expected_model`, integer `expected_generation: 2`
and exactly `switch_ids: [0]` are mandatory. Timeout is an integer 1-15 seconds, default 5; each response
is capped at 65,536 bytes.

Each invocation uses complete Remote Procedure Call (RPC) frames through `POST /rpc`, with source
`switchboard-native`, per-invocation integer request identifiers and fixed methods only:
`Shelly.GetDeviceInfo` with `ident: false`, `Switch.GetStatus` with `id: 0`, and, for controls,
`Switch.Set` with only `id: 0` and boolean `on`. Responses must match request identifier, expected source
identity and any returned destination; exactly one object-valued result or valid error frame is required.
`Shelly.GetDeviceInfo` is public and is checked afresh against the configured identity/model/generation.
The adapter requires a boolean `auth_en` matching whether a password reference is configured.

Passwords are optional for read-only unprotected devices, but **mandatory for native controls**.
Only bounded `env:NAME`, absolute `file:/...` or home-relative `file:~/...` references are accepted.
There are no literal, command, cloud-token, Basic-auth or WLED credential settings. `Context.secret`
resolves a reference only after a validated protected-read challenge. Passwords must be nonempty,
at most 1,024 bytes and contain no carriage return, newline or null byte. Errors and inventory withhold
resolved values and references.

A protected read first obtains a definite `401`. The adapter requires a bounded `Digest` challenge:
realm exactly equals the pinned device identity, algorithm `SHA-256`, and quality of protection exactly
`auth`. Numeric/hexadecimal and current base64-like token nonces are accepted; optional opaque/stale
directives, the character-set value `charset=UTF-8` and non-userhashed forms are narrowly checked.
Duplicate/unknown directives, different realms, downgrade algorithms, `auth-int`, multiple authentication
schemes and malformed shapes fail closed before credential lookup. Only then is the unchanged request
supplied once with `digest_password` and `digest_realm` pinned to `expected_device_id`.

The native curl transport, not a JSON `auth` object or a hand-built password header, performs HTTP Digest
as `admin` for the actual `POST /rpc` body. A configured password is not silently ignored when an unsigned
protected read succeeds. A second unauthorized response stops. A control requires successful authenticated
preflight and goes directly to the Digest-enabled transport; the adapter never retries `Switch.Set`.
The shared transport's bounded authentication negotiation after a definite `401` can involve multiple
wire exchanges; "one write attempt" means one adapter action invocation, not one network packet.
The adapter checks unsigned read challenges using the shared `validate_digest_challenge` parser in
[digest.cpp](../src/digest.cpp). The transport must apply that same parser with the pinned `digest_realm`
to every credentialed challenge before curl can automatically resend credentials. A changed nonce is
permitted only within the same realm and strict profile; no separate adapter/transport parser is used.
Curl owns subsequent nonce counts and handshakes. Fake transport and helper tests do not verify actual
callback ordering, curl's negotiation/body replay or real device enforcement.

### Nonessential lighting opt-in

After physically reviewing the attached load and existing timers, schedules, scripts and webhooks, and
only for nonessential lighting rated at most 100 watts, change both device settings together:

```json
{
  "allow_control": true,
  "controls": {
    "0": {
      "safe_load": "nonessential_lighting",
      "max_power_watts": 60,
      "safety_acknowledged": true
    }
  }
}
```

This is a fragment to merge into the already configured device, not a complete configuration.
The power limit must be an integer 1-100 watts. Never label heaters, cooking appliances, pumps, motors,
medical equipment, locks, doors, security equipment, infrastructure or safety-critical lighting as this
load type. This is not an emergency-stop interface or an electrical safety mechanism.

Immediately before either ON or OFF, identity and component status are rechecked. Control refuses absent,
negative or above-limit power, component errors, and a reported active timer. Missing power is never
assumed zero. A meter reading has no verified age, and an off load cannot reveal its next draw.
The adapter cannot inspect the physical appliance or certify the safety attestation. It does not inspect
or modify timer configuration, scripts, schedules or webhooks; an absent active-timer status does not
prove that no future automation exists. Review the load again whenever it changes.

Status returns output plus optional power in watts (`W`), cumulative consumed/returned energy in
watt-hours (`Wh`), voltage in volts (`V`) and current in amperes (`A`). Each measurement has `available`,
`value` and `unit`; absent/null values remain unavailable. Invalid/nonfinite/out-of-bound measurements
reject the response. Energy uses `aenergy.total`/`ret_aenergy.total`, not minute counters in different
units. It is a resettable device counter, not billing, a historical chart or a reliable interval delta.
`observed_at` is host time; `measurement_age_seconds` is unknown. Unrecognized provider error names
become `unrecognized_error`, and arbitrary provider strings are discarded.

## Results, uncertainty and native-versus-Python coverage

After one write attempt both adapters try one readback, **including after command uncertainty**.
Neither substitutes preflight data for readback, retries an ambiguous action, automatically corrects
unsafe output, or claims exactly-once effects. No generic successful handler envelope proves delivery.
Preflight failures raise a sanitized error before any write, rather than returning an attempted-action report.

- WLED separates `acknowledged` from `matches_requested`, `command_error` and `readback_error`.
  A correct acknowledgement plus matching snapshot yields `status: "state_matched"`; mismatch yields
  `state_mismatch`; accepted but unavailable readback yields `unconfirmed`. A provider rejection is
  `rejected`; malformed/missing acknowledgement or transport uncertainty remains `uncertain` even if
  readback matches. Color comparison normalizes a missing fourth channel to zero.
- Shelly separates `command_accepted`, previous `was_on`, `matches_requested` and `readback_safe`.
  Acceptance plus matching, safe metered state yields `state_matched`; a mismatch is `state_mismatch`;
  matching output with unsafe/unavailable power, component error or timer yields `unsafe_readback`.
  Missing readback after acceptance is `unconfirmed`. Definite authentication refusal is `rejected`;
  malformed, provider-error or missing acceptance remains `uncertain`, even with matching state.
- `physical_output_verified` and `automatic_resend` are always false. A matching state can predate the
  request or change immediately afterward; it proves neither causation, a completed fade, actual light
  output nor appliance operation. Use local/manual handling for abnormal conditions, not automatic
  corrective commands.

| Area                   | Native coverage                                                                         | Difference from the Python references                                                                          |
| ---------------------- | --------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| WLED absolute controls | Segment/global power and brightness, fixed segment colors, global persistent transition | Extra `allow_control` opt-in and brightness/transition caps; tools registered only for available scopes        |
| WLED reviewed presets  | Fingerprinted static copies only                                                        | Printable seven-bit preset metadata; separate segment/global/preset bindings                                   |
| WLED action reporting  | Acknowledgement and one readback, including uncertainty                                 | No `wled.action_result` event; no Python result-shape or fingerprint compatibility promise                     |
| Shelly reads           | Exactly the two reviewed generation 2 plugs and component 0                             | Python permits protocol-based read-only generation 2/3/4 models; native explicitly does not                    |
| Shelly output          | The same two plug models, absolute boolean only                                         | Native additionally requires password-backed preflight, rejects active timers and checks readback power safety |
| Secrets/authentication | Lazy `env:`/`file:` and native HTTP Digest transport                                    | No Python `cmd:` references or Python-managed nonce/session implementation                                     |
| Persistence/history    | Core-native approval/audit storage only                                                 | No shared Python database, connector tables, energy history, events, polling or offline telemetry fallback     |

Neither implementation is used as a fallback for the other. Discovery, arbitrary addresses/JSON/methods,
toggle, dynamic effects, segment geometry, playlists, scripts, cover/motor profiles, factory reset,
firmware changes, cloud control, counter resets and generic electrical-load control are not exposed.

## Authored checks and sources

The 69 named cases use `FakeHttp`, direct registry handlers and the shared Digest validator.
They specify independent/offline selection,
strict settings/schemas, exact methods/paths/payloads, per-device consent, optional native Digest setup,
lazy credentials with required pinned realms, shared-parser nonce rotation/realm-change/downgrade checks,
secret/error minimization, scope-bound required fingerprints, changed configuration,
safe-load/capability/cap checks, preset validation, response limits, provider errors, readback mismatch
and uncertainty without automatic replay. Matrix loops are additional scenarios, not additional named
case counts. Future test artifacts use private project-local directories, never a temporary-directory
helper. The tests have not been collected or executed.

They do **not** exercise the real dispatcher approval queue, approval restart/replay protection,
kill switch, policy overrides or rate accounting; those belong to native core coverage. They also do
not verify real curl headers/Digest negotiation, linking, endpoint resolution, actual credentials,
firmware versions, concurrent controllers, metering age or physical safety.

Provider references inspected for this implementation:

- [WLED JSON interface](https://kno.wled.ge/interfaces/json-api/): combined state/info, fields, capability
  arrays, transition units, notification suppression and acknowledgements.
- [WLED presets](https://kno.wled.ge/features/presets/): presets file, identifiers and macro/playlist risks.
- [Shelly RPC protocol](https://shelly-api-docs.shelly.cloud/gen2/General/RPCProtocol/) and
  [HTTP channels](https://shelly-api-docs.shelly.cloud/gen2/General/RPCChannels/): complete frames over `/rpc`.
- [Shelly authentication](https://shelly-api-docs.shelly.cloud/gen2/General/Authentication/) and
  [device information](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Shelly/#shellygetdeviceinfo):
  public identity, `auth_en`, HTTP Digest and nonce shapes.
- [Shelly switch](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Switch/):
  absolute output, previous `was_on`, status, timers, errors and metering units.
- Plus Plug S [component reference](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen2/ShellyPlusPlugS/)
  and [model identity](https://kb.shelly.cloud/knowledge-base/shelly-plus-plug-s); Plus Plug US
  [component reference](https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen2/ShellyPlugUS/) and
  [model identity](https://kb.shelly.cloud/knowledge-base/shelly-plus-plug-us).

Python source references are [WLED](../../src/switchboard/connectors/wled/) and
[Shelly](../../src/switchboard/connectors/shelly/), with their
[WLED documentation](../../docs/connectors/wled.md) and
[Shelly documentation](../../docs/connectors/shelly.md). Those references describe Python behavior;
the narrower native boundaries above are intentional.

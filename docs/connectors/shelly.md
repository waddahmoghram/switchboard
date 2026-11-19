# Direct Shelly connector

This opt-in connector reads explicitly configured Shelly generation 2, 3 or 4 switch components directly.
It can additionally control two narrowly allowlisted generation 2 plug models, but only for acknowledged,
low-power, nonessential lighting.
It does not use a cloud account or another home-automation connector.

**Validation status: all Shelly tests are authored and UNRUN.
No device/account endpoint was contacted.
No application launch, test collection or test execution was performed.
Public documentation inspection and scoped static checks are not hardware validation or proof that a command works on your firmware.**

## Scope and compatibility

- Read-only protocol support: devices reporting generation 2, 3 or 4 that implement the documented `Shelly.GetDeviceInfo` and `Switch.GetStatus` methods.
  Pin the exact identity, model, generation and component identifiers.
  A matching protocol is a compatibility prerequisite, not model certification.
- Control allowlist: generation 2 Shelly Plus Plug S (`SNPL-00112EU`) and Shelly Plus Plug US (`SNPL-00116US`), only `switch:0`.
  The [Plug S reference][plug-s] and [Plug US reference][plug-us] identify this component; their [product identities][identity-s] and [identity-us] verify the model codes.
- Other generation 2/3/4 models are read-only, including relays and multi-channel devices.
  A component without metering may still provide output status; missing power and energy are explicitly unavailable.
- Generation 1, future generations, cover/motor profiles, dimmers, lights, standalone power/energy meters,
  battery-only sensors and other component namespaces are unsupported.
  No discovery, scripting,
  configuration changes, counter resets, factory reset, arbitrary method invocation or cloud fallback.
- Documented old hexadecimal and newer token nonces are supported for Digest authentication.
  Unsupported authentication algorithms, realms or challenge formats fail closed.
  No firmware revision has been exercised; the official documentation notes feature freeze at `1.7.x` for the two control models.

## Setup

1. Register `"shelly": "switchboard.connectors.shelly"` in `KNOWN_CONNECTORS` in [the application assembler](../../src/switchboard/app.py).
   This root wiring is a separate merge step; this connector does not modify or dynamically patch the root registry.
1. In the device's existing local administration interface, verify its identity, generation, model, authentication setting and switch identifiers.
   Reserve its address in your router.
   Never copy a cloud key into this connector.
   `Shelly.GetDeviceInfo` is public even with authentication enabled; the connector explicitly requests `ident = false` and does not return the raw identity response.
1. Merge [the complete root configuration snippet](../config/shelly.toml) into your configuration, preserving other enabled connectors.
   Replace its synthetic address and identity.
   The snippet starts read-only: `allow_control = false` and no active `controls` table.
1. Enable the device password using the device's administration interface and arrange the referenced environment variable or an existing `file:`/`cmd:` secret reference.
   Registration and `doctor` do not resolve secrets or contact devices.
   A successful `doctor` proves configuration validity only.
1. Keep control disabled unless the physical load satisfies every restriction below.
   Only then change `allow_control` to `true` and uncomment the entire component-specific safety table.
   Keep the action policy at `ask`; the sample also caps successful/in-flight actions at four per rolling hour.

Use the fixed root origin, not a `/rpc` path or a method endpoint.
Origins cannot contain credentials, paths other than a trailing slash, queries, fragments, whitespace or escape sequences.
There are at most 16 named devices, 16 explicit switch identifiers per device, and switch identifiers from 0 through 31.
Aliases are bounded lowercase names.
Unknown settings, duplicate identities/endpoints and mistyped booleans are rejected.
The per-socket timeout must be a whole number from 1 through 15 seconds;
this is not an end-to-end operation deadline.

### Transport and authentication

The transport is Hypertext Transfer Protocol (HTTP), using `POST /rpc` and a complete [Remote Procedure Call (RPC) frame][protocol] encoded as JavaScript Object Notation (JSON).
There is no method-specific query transport or WebSocket connection.
Responses must match the request identifier, configured source identity and optional destination.
Redirects are never followed, including same-origin redirects.
Every response is bounded to 65,536 bytes; errors contain fixed descriptions, never provider messages, addresses, headers, challenge bodies or credentials.

Stock devices typically expose plain HTTP; the official [authentication reference][auth] describes secure sockets for outbound connections, not an inbound encrypted listener.
An `https://` origin is for an operator-managed, trusted Transport Layer Security (TLS) endpoint that preserves the device's Digest challenge and RPC response.
Certificate verification remains enabled; there is no encrypted-to-plain fallback or certificate bypass.

Plain HTTP requires explicit `allow_http = true`, even on a private address.
It additionally requires a numeric address in `10/8`, `172.16/12`, `192.168/16` or `fc00::/7`; hostnames, loopback, link-local, multicast and public numeric targets are refused for plain HTTP.
This prevents name rebinding there but does not prove ownership.
Keep the device isolated on your own trusted network and out of public reach.
The shared web client honors environment proxies: configure proxy exclusions for device addresses;
the connector does not change process-wide proxy settings.

[HTTP Digest authentication][auth] uses the fixed user `admin`, `SHA-256`, and quality of protection `auth`.
A protected read obtains a `401` challenge, then resolves the bounded password reference lazily.
The digest hashes the actual `POST:/rpc` transport, not WebSocket dummy values.
A session increments its nonce count and generates a fresh client nonce; sessions and resolved passwords are not persisted.
A configured password is never silently ignored: disabled authentication or a protected operation succeeding without a challenge is an error.
When authentication is configured, a control is never sent unsigned; its authenticated preflight must succeed first.

Only a definite `401` with a valid new challenge permits one authentication negotiation attempt.
A repeated nonce after signed rejection or a second `401` stops.
There is no algorithm downgrade, unbounded nonce refresh, throttling retry or retry after transport uncertainty.
The connector does not change the device's authentication configuration.
Captured HTTP traffic still reveals telemetry and Digest material and can enable password guessing; Digest is not a substitute for network isolation.

## Tools and approval

Literal policy tiers `READ` and `ACT` have their normal core semantics.
`READ` is allowed by default;
`ACT` asks assistants and automation rules for approval.
Direct human invocation counts as approval,
and operator policy can override defaults.
The kill switch, denial rules and rate caps remain effective.

| Tool                | Tier   | Arguments                                  | Behavior                                         |
| ------------------- | ------ | ------------------------------------------ | ------------------------------------------------ |
| `shelly_devices`    | `READ` | None                                       | Offline aliases, components and control targets. |
| `shelly_status`     | `READ` | `device`, `switch_id`                      | One current permitted component, no cache.       |
| `shelly_switch_set` | `ACT`  | `device`, `switch_id`, `target`, `on`      | Absolute output plus one post-command readback.  |

`shelly_switch_set` is not registered at all when there are no opted-in controls.
Tool schemas reject unknown properties and targets before approval; handlers additionally enforce the per-device component allowlist, including cross-device combinations that a flat schema cannot express.

Obtain `target` from the chosen component in `shelly_devices`.
It is a 64-character fingerprint of the configured alias, normalized origin, pinned identity/model/generation, component, transport consent, authentication reference and safe-load policy.
It is not a selectable network address.
Both schema and handler verify it.
Pending approvals cannot silently retarget when these settings change or when controls are withdrawn; list devices and request a new approval after a configuration change.

For example, after listing the configured devices, a proposed action has this shape:

```json
{
  "device": "desk_lamp",
  "switch_id": 0,
  "target": "<exact current 64-character fingerprint from shelly_devices>",
  "on": false
}
```

The placeholder is explanatory and is not accepted as a real target.
There is no toggle, flip-back timer, arbitrary parameter dictionary, endpoint or method argument.
The only write payload parameters are the configured component's integer `id` and the explicit boolean `on`.

### Safe-load restrictions

Both device opt-in and a component-specific `safety_acknowledged = true` are mandatory.
The sole accepted load classification is `nonessential_lighting`.
The operator must physically verify a load's rating and set `max_power_watts` to an integer from 1 through 100.
The limit cannot be raised above 100.
Never configure heaters, cooking appliances, refrigeration, pumps, motors, high-power equipment, medical/life-support equipment, security equipment, cameras, locks, doors, network infrastructure or safety-critical lighting as nonessential lighting.

Immediately before any write, the connector rechecks identity and obtains component status.
It refuses both ON and OFF when power is unavailable, negative, above the configured limit, or the component reports an error.
These are conservative software checks, not an electrical safety mechanism: an output that is off cannot reveal the next load's draw, a meter reading has no verified age, and software cannot verify the attached appliance or its nameplate.
A false safety attestation defeats this safeguard.
Use local/manual control for abnormal conditions, not this connector as an emergency stop.

The connector does not inspect, disable or alter existing device timers, schedules, scripts or webhooks.
Do not opt in a plug whose existing automation or attached load can create unsafe effects.
The owner must recheck the physical load whenever it changes.
Readback is an immediate observation, not a promise of lasting state, physical appliance operation or protection against concurrent controllers.

## Results, units and uncertainty

Each successful status response contains a host-side `observed_at` timestamp, `cached = false`, and
`measurement_age_seconds = null`: the documented status does not establish when the power sample was
taken. Failed reads raise a sanitized tool error; there is no cached, empty or zero-valued fallback.

- `power`: instantaneous `apower`, unit `W` (watts).
- `energy`: `aenergy.total`, unit `Wh` (watt-hours), marked `device_cumulative_counter`.
- `returned_energy`: optional `ret_aenergy.total`, also `Wh`.
- `voltage` and `current`: optional values in `V` (volts) and `A` (amperes).
- Each measurement has `available`, `value` and `unit`. Absent/null values are unavailable, not zero.
  Non-finite, mistyped or out-of-bound measurements reject the status response.
- Energy counters can reset and are not billing, lifetime consumption, a time-window summary or a
  reliable delta across restarts. `aenergy.by_minute` is deliberately not used: its documented unit is
  milliwatt-hours, unlike `total`, and it also depends on the device clock.
- Known component error names are retained; unknown provider strings become `unrecognized_error`.
  Source tags, names, private identity fields and provider error messages are not returned.

The [switch reference][switch] defines `Switch.Set.result.was_on` as the **previous** output, not proof
that the requested output was observed. Control reports separate:

- `command_accepted`: `true` for a correctly shaped acceptance, `false` for a definite authentication
  refusal, or `null` when the result is uncertain, including malformed replies and provider errors.
- `observation`: one post-command `Switch.GetStatus` result, or `null` with a safe `readback_error`.
  It never substitutes the preflight state for a failed readback.
- `matches_requested`: whether that observation equals the requested output, or `null` if unknown.
- `ok`: true only when acceptance is known, readback matches and the readback has no component error.
  `status` is `confirmed`, `unconfirmed` or `uncertain`; `automatic_resend` is always false.
  That flag excludes the bounded negotiation after a definite `401`; it prohibits uncertainty-driven retries.
- `confirmed` equals `ok`; `effect_unknown` is true when `command_accepted` is `null`. The core action
  journal then records the call as `uncertain` and refuses an identical request until you check the plug
  and run `switchboard actions resolve`.

After an uncertain write, the connector may read status but never blindly resends the command.
An output already matching the request does not prove that the command executed. Inspect the state and
decide whether a new, explicit approval is warranted.

**The core's outer `status = "ok"` and an approval state of `executed` mean the handler returned a
report, not that the plug changed.** Inspect the report's `ok`, acceptance and observation fields before
claiming success or composing a follow-on action. No automatic multi-step workflow is provided.

## Architecture, persistence and tradeoffs

[Settings](../../src/switchboard/connectors/shelly/settings.py) validate configuration offline.
The [client](../../src/switchboard/connectors/shelly/client.py) performs only three fixed methods.
[Handlers](../../src/switchboard/connectors/shelly/service.py) minimize readings and enforce controls.
[Registration](../../src/switchboard/connectors/shelly/__init__.py) adds only tools.

There are **no connector events, background jobs, subscriptions, named services, telemetry snapshots or energy summaries**, and `MIGRATIONS = []`.
This deliberately avoids a second control path around the dispatcher and any claim of continuous monitoring or atomic snapshot/event delivery.
Existing core audit and approval storage persists action intent and returned approval reports; direct status readings are not retained.
There is no connector database upgrade or background network work at startup.
The tradeoff is extra identity/authentication requests and no historical charts, change notifications or offline readings.
A process crash after a device write can still leave the physical result uncertain; core approval claiming is not an exactly-once device guarantee.

## Authored checks and evidence limits

The synthetic cases in `tests/unit/test_shelly_*.py`, `tests/integration/test_shelly_flow.py`, and
`tests/fixtures/shelly/` cover offline registration, opt-in/root wiring, strict configuration and schemas,
cross-device target restrictions, identity pinning, authentication challenge variants, nonce counts,
secret/error redaction, redirects, response bounds, measurement units/unavailability, safe preflight,
absolute payloads, acceptance/readback mismatches, transport uncertainty, approval replay, configuration
changes, kill switch, denial policy and rate caps. Integration cases use the real core dispatcher and
`FakeHttp`, not a listener or physical device.

**These are UNRUN specifications, not passing tests.** Static analysis cannot verify firmware behavior,
network/proxy behavior, credentials, the actual load, real readback timing or device safety.

## Primary references

Official references were inspected on 2026-11-11:

- [RPC protocol and response frames][protocol].
- [HTTP RPC channels: complete-frame and method-specific transports][channels].
- [Authentication: HTTP challenges, current nonce handling and legacy behavior][auth].
- [Device identity and `ident` information sensitivity][device-info].
- [Switch status, units, errors, absolute output and `was_on`][switch].
- [Shelly Plus Plug S component support][plug-s] and [model identity][identity-s].
- [Shelly Plus Plug US component support][plug-us] and [model identity][identity-us].
- [HTTP semantics][http-semantics] and [TLS terminology][tls-spec].

[protocol]: https://shelly-api-docs.shelly.cloud/gen2/General/RPCProtocol/
[channels]: https://shelly-api-docs.shelly.cloud/gen2/General/RPCChannels/#http
[auth]: https://shelly-api-docs.shelly.cloud/gen2/General/Authentication/
[device-info]: https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Shelly/#shellygetdeviceinfo
[switch]: https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Switch/
[plug-s]: https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen2/ShellyPlusPlugS/
[plug-us]: https://shelly-api-docs.shelly.cloud/gen2/Devices/Gen2/ShellyPlugUS/
[identity-s]: https://kb.shelly.cloud/knowledge-base/shelly-plus-plug-s
[identity-us]: https://kb.shelly.cloud/knowledge-base/shelly-plus-plug-us
[http-semantics]: https://www.rfc-editor.org/rfc/rfc9110.html
[tls-spec]: https://www.rfc-editor.org/rfc/rfc8446.html

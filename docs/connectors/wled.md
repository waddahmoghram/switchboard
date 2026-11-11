# Local WLED

This opt-in connector controls operator-owned WLED lights directly, without a cloud account, Home Assistant,
or additional runtime packages. Its JavaScript Object Notation (JSON) application programming interface (API)
surface is deliberately smaller than WLED's. It cannot discover devices, toggle, choose random effects,
change segment geometry, execute macros, manage firmware, reset configuration, or send arbitrary commands.

**Verification boundary:** implementation and synthetic tests were authored without importing or running
Switchboard, collecting or executing tests, starting servers, or contacting a light. Official documentation
was inspected on 2026-11-11; firmware compatibility and physical behavior remain unverified.

## Trust and setup

Typical local WLED deployments do not authenticate state requests. Knowing a reachable address can be enough
to control the lights. A settings password is not a substitute for authenticating this interface. The
connector deliberately has no token, password, or secret-reference setting; it cannot authenticate a
reverse proxy that requires credentials.

Keep the device and Switchboard on your own isolated network. Restrict network access with firewall rules;
do not forward the device's ports or expose it to the Internet. Plain Hypertext Transfer Protocol (HTTP)
requests are unencrypted and require `allow_http = true` on **each** device, including loopback endpoints.
Use HTTP over Transport Layer Security where a local endpoint supports it; certificate validation remains
enabled. Do not disable certificate checks.

1. Inspect your device in its own interface. Record its fixed private address and the segment identifiers
   you actually intend to expose. Start with one harmless segment and low brightness.
1. Review and merge [the example configuration](../config/wled.toml) into your own configuration. Merely
   having a `[connectors.wled]` table does not activate anything: add `wled` to
   `[switchboard].connectors` explicitly, preserving other enabled connectors.
1. Leave `allow_global = false` initially. When true, whole-device controls affect **all** segments,
   including segments omitted from `segments`. The segment list limits segment-targeted writes and the
   segment details returned to assistants; it is not a global-action filter.
1. Check existing policy overrides and tool exposure. The example asks for all WLED actions. Exact
   per-tool overrides can take precedence over a wildcard. Keep approvals enabled.
1. Ensure local traffic bypasses any configured environment proxy, for example by setting `NO_PROXY` and
   `no_proxy` for the chosen address on every process host. The connector uses the shared transport and
   does not introduce a separate proxy policy.
1. Restart every Switchboard process after configuration changes. Configuration is loaded per process,
   not watched or hot-reloaded. Old processes retain their original fixed targets; newly loaded
   configuration rejects approvals bound to the old configuration.

Accepted origins are private and loopback literals, Internet Protocol version 4 (IPv4) link-local literals,
Internet Protocol version 6 (IPv6) unique-local literals, `localhost`, and names ending in `.local` or
`.home.arpa`. Only an origin is allowed: no credentials,
path prefix, query, fragment, percent encoding, or alternate scheme. Hostnames are operator-trusted and
resolved by the shared transport at operation time, not pinned to a physical device. Prefer fixed private
addresses when that distinction matters. Public addresses, discovery, and model-supplied addresses are refused.

The following are **recommended user-run steps, not commands executed during implementation**:

```bash
switchboard doctor
switchboard call wled_devices
switchboard call wled_status --args '{"device":"desk"}'
```

WLED's `doctor` checks settings offline only. A successful result does not prove reachability, authentication,
firmware support, or that a light is safe to activate. Other enabled connectors have their own doctor behavior.
Read tools return the current `target_digest`. Copy the value exactly into a proposed action:

```json
{
  "device": "desk",
  "target_digest": "<copy the current 64-character digest from wled_devices or wled_status>",
  "segment": 0,
  "on": false
}
```

Use those arguments with `wled_set_power`. Assistant and rule calls wait for approval by default; review
the exact device, segment, operation, and values before approving. A direct `switchboard call` is a human
invocation and treats typing an action as approval. Do not use it as an approval-queue demonstration.

Settings reject unknown fields. Configure 1-16 uniquely named devices, each with 1-16 distinct exposed
segment identifiers in 0-63 and at most 16 reviewed presets. Aliases use lowercase letters, digits and
underscores, begin with a letter, and are at most 32 characters. Preset identifiers are 1-250 and must be
unique within a device. Timeout values are whole seconds in 1-30; response limits are 4,096-262,144 bytes.
Defaults are five seconds per socket operation and 65,536 bytes, not a whole-operation deadline.

## Tools and scopes

`READ` and `ACT` below are literal policy tiers, not claims about physical success.

| Tool                  | Tier   | Arguments beyond `device` and action binding | Scope and behavior                               |
| --------------------- | ------ | -------------------------------------------- | ------------------------------------------------ |
| `wled_devices`        | `READ` | None; does not take `device`                 | Offline aliases, scopes, presets, current digest |
| `wled_status`         | `READ` | None; does not take an action binding        | One live state/capability snapshot               |
| `wled_set_power`      | `ACT`  | `on`, optional `segment`                     | Boolean only; no toggle                          |
| `wled_set_brightness` | `ACT`  | `brightness`, optional `segment`             | Integer 1-255; does not implicitly turn on       |
| `wled_set_color`      | `ACT`  | `segment`, `color`                           | Three or four byte channels; sets Solid effect   |
| `wled_set_transition` | `ACT`  | `transition_deciseconds`                     | Whole-device persistent setting, 0-100           |
| `wled_apply_preset`   | `ACT`  | `preset`                                     | One configured reviewed static preset alias      |

Every action requires `target_digest`; read tools do not. Unknown fields and out-of-range values are
rejected. All targets are named configuration entries, never addresses supplied as tool arguments.
Brightness zero is refused: use explicit `on = false`. Color writes affect the primary color slot and
explicitly set `fx = 0`, WLED's Solid effect. They do not turn on the global or segment power state.
The optional fourth channel requires reported white-channel support; color operations also require
reported red/green/blue support. White-only and color-temperature-only control are not implemented.

Transitions use WLED's documented deciseconds, not milliseconds: `4` means 400 milliseconds. The connector
caps the persistent `transition` setting at ten seconds rather than depending on the documentation's
inconsistent upper-limit wording. It does not send the one-call `tt` field because that field is absent from
state responses and cannot be verified by readback.

Live capability checks use `info.leds.seglc` by **segment identifier**, not the deprecated global
`rgbw`/`wv` fields or the intersection `lc`. Segment writes require active reported bounds, a reported
`maxseg`, and the corresponding reported control. All writes refuse active or unreported realtime,
playlist, or nightlight modes rather than silently overriding them. Missing capabilities remain unknown;
the connector does not invent support for older firmware.

## Approval binding

The binding is the lowercase hexadecimal result of `hashlib.sha256` over
`canonical_json(...).encode("ascii")` for this object:

```json
{
  "engine": "wled_native_static_v1",
  "name": "<device alias>",
  "base_url": "<normalized configured origin>",
  "allow_http": false,
  "allow_global": false,
  "segments": ["<sorted configured integer identifiers>"],
  "presets": [{"name": "<alias>", "id": 1, "sha256": "<reviewed object fingerprint>"}],
  "timeout_seconds": 5,
  "max_response_bytes": 65536,
  "max_transition_deciseconds": 100,
  "max_segments": 64,
  "suppress_udp_notification": true
}
```

`canonical_json` sorts object keys, uses compact separators, and escapes characters outside the seven-bit
character set, with no trailing newline.
Presets are sorted by alias; their identifiers and fingerprints are included. The example above is a
shape description, not a literal digest input. No resolved secret values are included; none are used.
The action schema requires the digest and enumerates current device digests. The handler also checks
that it belongs to the **requested device**, before any network request. Core approval execution validates
the stored arguments again against the newly registered schema after a restart.
Changes to engine behavior or fixed safety rules require a new engine tag when maintaining this connector.

Changing the endpoint, alias, segment set, whole-device permission, transport consent, any reviewed preset,
or either transport limit invalidates the device's old action binding. This is intentionally coarse:
adding an unrelated preset also invalidates pending actions for that device. Read again and obtain fresh
approval; do not silently substitute the new digest in an old approval. The binding is not authentication
or a physical-device identity, and does not freeze state changed independently on the light.

## Reviewed presets without macro execution

WLED presets can contain arbitrary API commands and playlists. Allowing a preset identifier alone would
therefore reopen commands intentionally excluded from these tools. This connector instead fetches
`/presets.json`, validates and fingerprints the selected object, and copies only supported static state
fields into its own bounded write. It **never posts `ps`**, so the firmware does not execute or re-read a
preset after review. A file change between review and sending cannot replace the copied state with a macro.
Consequently, WLED's reported `ps` is not proof of this operation and need not equal the source identifier.

Create a deliberately small preset command in your device's own interface, for example:

```json
{"n":"Warm desk","seg":[{"id":0,"on":true,"bri":96,"col":[[255,160,40]],"fx":0}]}
```

Allowed top-level fields are `n`, `ql`, `on`, `bri`, `transition`, and `seg`. Segment fields are explicit
`id`, `on`, `bri`, `col` (one to three colors), and `fx` (zero only). Metadata `n`/`ql` is fingerprinted but
not sent. Every color-bearing segment must explicitly include `fx = 0`. Geometry, relative values,
random/toggle commands, nested presets, playlists, synchronization settings, resets, saving/deleting
presets, and all unknown fields are refused even if the fingerprint matches. A segment-only preset may
work with `allow_global = false`; top-level state controls still require global permission.

Ordinary presets saved with full current segment configuration often contain unsupported fields. They
are intentionally rejected, not partially applied. After manually reviewing the **entire** selected
object, compute its fingerprint on your own machine. These steps contact your own configured device and
were not executed here:

```bash
curl --fail --silent --show-error --max-time 10 --noproxy '*' \
  http://192.168.50.20/presets.json \
  --output ./wled-presets-review.json
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

selected = json.loads(Path("wled-presets-review.json").read_text(encoding="utf-8"))["1"]
print(json.dumps(selected, indent=2, ensure_ascii=True))
canonical = json.dumps(selected, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
print(hashlib.sha256(canonical.encode("ascii")).hexdigest())
PY
```

Add a reviewed entry to that device's `presets` array, replacing the fingerprint placeholder:

```toml
presets = [
  { name = "warm_desk", id = 1, sha256 = "<reviewed lowercase 64-character fingerprint>" },
]
```

The full presets file is byte-limited even though only one entry is selected. If legitimately necessary,
raise `max_response_bytes` from 65,536, up to 262,144; this also changes the target binding. A new review is
required after **any** selected-object edit, including metadata. Do not automatically refresh fingerprints.

## Architecture, results, and tradeoffs

Registration and the WLED doctor perform only strict configuration validation. There are no migrations,
connector tables, polling jobs, event subscribers, named services, or secret lookups. The connector uses
the core transport, schema validator, approval gate, audit log, and durable event queue.

Each operation is bounded to configured targets. Reads use `GET /json/si`, which combines state and info
without downloading effect or palette catalogs. Presets additionally use `GET /presets.json`.
Approved controls read a capability snapshot, validate scope and overriding modes, make exactly one
`POST /json/state` attempt, and perform one readback after a non-error response. Every request disables
redirect following, has a per-socket timeout and response byte cap, and honors per-device HTTP consent.
Error bodies, network exception details, network identifiers, and arbitrary device metadata are withheld.

Writes add `{"udpn":{"nn":true}}` to suppress the documented one-call datagram synchronization broadcast.
This is not a guarantee against every firmware user extension, external automation, wired connection,
or independently configured synchronization side effect. Network isolation and operator review still matter.

After an attempted write, `wled.action_result` records the same bounded result returned to the caller.
Its `requested` field contains the typed state patch, without addresses. Important result fields are:

- `status = "confirmed"` and `confirmed = true`: a subsequent snapshot matched the requested fields,
  including the Solid effect when requested. This is observed target state, not measured physical output,
  completed fades, causation, or confirmation that unrequested state stayed unchanged.
- `status = "unconfirmed"`: readback mismatched or was unavailable. `acknowledged = true` alone is never
  confirmation. A malformed acknowledgement can still be followed by a matching snapshot.
- `status = "error"`: the write attempt had a transport, redirect, provider, or HTTP error. The external
  effect remains unknown, even if the device reported failure. There is no automatic retry.
- `stale = true`, `observation = null`: no usable post-write observation exists. A preflight snapshot is
  never presented as readback. Successful live snapshots use `stale = false` only as of their observation time.

Preflight failures raise a tool error before writing and produce no `wled.action_result`. A failure to
persist an event after a write cannot undo the device effect. Core `status = "ok"` and an approval marked
`executed` mean the handler returned a result, **not** that the light complied; consumers must inspect
`confirmed` and the nested result status. Rules should not announce success merely because approval ran.

There is deliberately no polling, cached status fallback, full history, WebSocket connection, background
retry, or exactly-once effect promise. Independent writes can race a preflight or readback. A snapshot can
already be superseded, a matching value can predate the request, and the device can misreport its state.
The conservative static subset sacrifices effect, playlist, preset-indicator and older-firmware
compatibility in exchange for auditable, limited commands.

## Authored checks and sources

The synthetic [settings tests](../../tests/unit/test_wled_settings.py),
[tool tests](../../tests/unit/test_wled_tools.py), and
[approval integration tests](../../tests/integration/test_wled_approval.py) cover offline registration,
strict settings and arguments, exact payloads and endpoint confinement, redirects and errors, scoped
capabilities, acknowledgements versus readback, static preset pinning, and stale-configuration approval
rejection after rebuilding the application. They are **UNRUN**; no test collection or execution is implied.
Static syntax, lint, formatting, type, and documentation-data checks are not device or runtime validation.

Official references inspected for this implementation:

- [WLED JSON API](https://kno.wled.ge/interfaces/json-api/): `/json/si`, state fields, per-segment
  `seglc`, color slots, `transition`/`tt`, `udpn.nn`, and acknowledgement behavior.
- [WLED presets](https://kno.wled.ge/features/presets/): `/presets.json`, identifiers 1-250, and the
  ability to store general commands or playlists.
- [Switchboard connector contract](../CONNECTOR_GUIDE.md): offline registration, policy tiers,
  bounded validation, transport safety, and event persistence.

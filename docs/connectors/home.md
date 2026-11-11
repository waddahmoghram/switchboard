# Home connector

The home connector reads and controls the Home Assistant entities you choose, through Home
Assistant's Representational State Transfer (REST) application programming interface (API). Locks,
alarm panels, covers (which include garage doors), and cameras stay blocked unless you deliberately
change the deny list, and every action waits for your approval by default.

## Tools

| Name                | Tier | Purpose                                                                           |
| ------------------- | ---- | --------------------------------------------------------------------------------- |
| `home_states`       | read | Exposed entities (or one of them) with state, last change, and a few attributes   |
| `home_call_service` | act  | One action on one exposed entity, for example `light.turn_on` for `light.kitchen` |

`home_states` takes an optional `entity_id`, an optional `domain` (such as `light`), and `limit`
(1–200, default 100). It returns only these attributes, when present: `friendly_name`,
`device_class`, `unit_of_measurement`, `current_temperature`, `temperature`, `hvac_action`, and
`brightness`. Everything else, such as a camera's access token, is left out.

`home_call_service` takes `domain`, `service`, `entity_id`, and an optional `data` object. The
domain must equal the entity's own domain, so an exposed light cannot be attached to an unrelated
action such as `homeassistant.restart`. The result names the action and lists the changed states
of exposed entities only. Its approval line reads, for example,
`Home Assistant light.turn_on on light.kitchen with {"brightness_pct":40}`.

### Allowed `data` keys (checked 2026-11-11)

`data` accepts only these keys, with the ranges shown. The names come from Home Assistant's action
reference, for example [light.turn_on](https://www.home-assistant.io/actions/light.turn_on/) and
[climate.set_temperature](https://www.home-assistant.io/actions/climate.set_temperature/).

| Key                                                  | Accepted values                   | Actions that use it                                   |
| ---------------------------------------------------- | --------------------------------- | ----------------------------------------------------- |
| `brightness_pct`                                     | integer 0–100                     | `light.turn_on`                                       |
| `brightness`                                         | integer 0–255                     | `light.turn_on`                                       |
| `color_temp_kelvin`                                  | integer 1,000–20,000              | `light.turn_on`                                       |
| `rgb_color`                                          | three integers 0–255              | `light.turn_on`                                       |
| `transition`                                         | 0–300 seconds                     | `light.turn_on`, `light.turn_off`, `light.toggle`     |
| `temperature`, `target_temp_low`, `target_temp_high` | −50 to 100                        | `climate.set_temperature`                             |
| `hvac_mode`                                          | a lowercase word such as `heat`   | `climate.set_temperature`, `climate.set_hvac_mode`    |
| `preset_mode`                                        | text, up to 64 characters         | `climate.set_preset_mode`, `fan.set_preset_mode`      |
| `fan_mode`                                           | text, up to 64 characters         | `climate.set_fan_mode`                                |
| `percentage`                                         | integer 0–100                     | `fan.set_percentage`                                  |
| `humidity`                                           | integer 0–100                     | `humidifier.set_humidity`                             |
| `volume_level`                                       | 0–1                               | `media_player.volume_set`                             |
| `is_volume_muted`                                    | true or false                     | `media_player.volume_mute`                            |
| `source`                                             | text, up to 100 characters        | `media_player.select_source`                          |
| `option`                                             | text, up to 100 characters        | `select.select_option`                                |
| `value`                                              | a number, text, or true or false  | `number.set_value`, `input_text.set_value`            |

Keys that choose targets (`entity_id`, `device_id`, `area_id`, `floor_id`, `label_id`) are refused,
so an action can touch only the entity named in `entity_id`.

## Events and services

The connector emits no events, subscribes to none, and provides and uses no services.

## Setup (checked 2026-11-11)

Facts from Home Assistant's [REST API page](https://developers.home-assistant.io/docs/api/rest/),
which now redirects to
[contributors.home-assistant.io](https://contributors.home-assistant.io/core/internals/rest-api/):

* The API is served under `/api/` on the same port as the web interface: 8123 by default.
* Every request needs an access token in the `Authorization` header (`Authorization: Bearer
  <token>`). For a script, create a long-lived access token on your profile, under **Security**.
* `GET /api/states` returns every state the user may read; `GET /api/states/<entity_id>` returns
  one, or 404.
* `POST /api/services/<domain>/<service>` runs an action and waits until it is done. The body is the
  action data, including the target, and the reply lists the states that changed. The connector
  never adds `?return_response`, which Home Assistant answers with 400 for actions without response
  data.
* Errors are 400, 401, 404, and 405; an action that fails returns 500, and every endpoint returns 503
  while Home Assistant shuts down.
* Some endpoints, such as `POST /api/events/<event_type>` and `POST /api/template`, need an
  administrator.

Steps:

1. In Home Assistant, sign in as a dedicated user that is **not** an administrator, so the token
   cannot use the administrator-only endpoints, and create a long-lived access token on that user's
   profile.
2. Store the token where a secret reference can reach it, for example a file with mode 600, and set
   `token = "file:~/.config/switchboard/secrets/home-assistant"`.
3. Set `base_url` to your server's `https://` address. A plain `http://` address is refused unless
   you also set `allow_http = true`, which is meant only for a server on your own network: plain
   Hypertext Transfer Protocol (HTTP) sends the token in clear text.
4. List the entities assistants may see in `expose`, as globs. Prefer exact entity identifiers over
   whole domains when a domain mixes harmless and sensitive devices.
5. Run `switchboard doctor`, then `switchboard call home_states`.

A complete example is in [docs/config/home.toml](../config/home.toml).

## Exposure and the deny list

An entity is visible only if a glob in `expose` matches it **and** its domain is not in
`deny_domains`. The deny list always wins. Its default is `["lock", "alarm_control_panel", "cover",
"camera"]`. Setting `deny_domains` replaces the default, so to allow covers you write the remaining
domains out explicitly, for example `deny_domains = ["lock", "alarm_control_panel", "camera"]`.

This connector's `expose` list is separate from the exposure settings Home Assistant keeps for its
own voice assistants.

## Suggested policy

```toml
[policy.tools.home_call_service]
decision = "ask"        # the default for act tools; written out so a broad glob rule cannot loosen it
max_per_hour = 20
```

## Limits and tradeoffs

* Requests go over the REST API one at a time. The connector does not follow live changes; Home
  Assistant recommends its WebSocket API for that, which this connector does not use.
* The deny list works by domain. Some integrations model a garage door opener as a `switch` or a
  `button`, so expose such entities only by exact identifier, after checking what they control.
* Scripts, automations, and scenes can do anything their steps do, including opening a lock that is
  on the deny list. Do not expose `script.*`, `automation.*`, or `scene.*` unless you have reviewed
  every one of them.
* `data` is limited to the keys above. Actions that need other fields are not available through this
  tool.

## Security notes

* The token is resolved for each request, sent only in the `Authorization` header, and never logged
  or returned; error messages describe the status code instead.
* Entity identifiers, domains, and action names are checked against strict patterns before they
  reach a request path, so they cannot change the address being requested.
* An approval stores the exact arguments the assistant proposed, so the entity and values cannot
  change between your approval and the call.
* Entity names and states come from your devices and integrations; treat them as untrusted data,
  never as instructions.
* To revoke access, delete the long-lived access token in Home Assistant, or deactivate the
  dedicated user.

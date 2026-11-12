# Contract fixtures

Each `*.json` file here except [synthetic-tools.json](./synthetic-tools.json) is a case file that both runners
evaluate: [tests/contracts/test_contracts.py](../../tests/contracts/test_contracts.py) for Python and
[native/tests/contracts.cpp](../../native/tests/contracts.cpp) for the native engine. The normative rules are in
the [contract](../README.md); a fixture only demonstrates them. Files use JavaScript Object Notation (JSON),
duplicate object keys are refused, and no fixture contacts an account, device or network service.

| File                                           | Area                                                                    |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| [schema.json](./schema.json)                   | Schema definitions and argument values in the supported keyword subset  |
| [policy.json](./policy.json)                   | Tier defaults, configured decisions, origins, kill switch, budgets      |
| [approvals.json](./approvals.json)             | Approve, deny, replay, changed policy, duplicate guard at approval      |
| [journal.json](./journal.json)                 | Result classification, duplicate guard, reconciliation, sweeps          |
| [exposure.json](./exposure.json)               | Tool exposure through each engine's protocol layer                      |
| [tasks.json](./tasks.json)                     | Shared subset of the local tasks tools                                  |
| [synthetic-tools.json](./synthetic-tools.json) | The `contract_*` tools both runners register, and what each mode does   |

## Case files

| Key                 | Meaning                                                                         |
| ------------------- | ------------------------------------------------------------------------------- |
| `format`            | `switchboard-contract-cases`                                                    |
| `version`           | Fixture format version, currently `1`; runners refuse versions they do not know |
| `contract_revision` | Contract revision the cases demonstrate, currently `1`                          |
| `area`, `summary`   | Description only                                                                |
| `cases`             | Cases, each with a file-unique `id`, a `summary` and `steps`                    |

A case may add `config`, the configuration both engines accept: `switchboard.connectors` (only `tasks` is
shared), `policy.tools.<name or glob>` with `decision` and `max_per_hour` (1 or more), and `mcp.expose`. Each
runner adds its own private temporary data directory. Every engine starts with the synthetic tools registered.

Steps run in order against one engine instance and stop at the first failed expectation. The engine starts on
the first step that needs it; `check_schema`, `validate` and `register` never start one.

| `op`                  | Fields                                                                         | Checks (`expect`)                                     |
| --------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `call`                | `tool`, `args` (default `{}`), `origin` (`assistant` default, `rule`, `human`) | Outcome envelope                                      |
| `approve`             | `approval`                                                                     | Outcome envelope                                      |
| `deny`                | `approval`                                                                     | `denied`: whether a pending request was denied        |
| `reconcile`           | `action`, `resolution` (`applied`, `not_applied`), `note`                      | `accepted`, and `state` after an accepted decision    |
| `action_state`        | `action`                                                                       | `state`, or `null` for no such entry                  |
| `recent_actions`      | `args` for `actions_recent`                                                    | `entries`, `count`, `present_fields`, `absent_fields` |
| `handler_calls`       | none                                                                           | Synthetic handler runs per tool name, across restarts |
| `kill_switch`         | `on`                                                                           | none                                                  |
| `restart`             | `config` (default: the case `config`)                                          | none; the same data directory is reopened             |
| `tick`                | none                                                                           | none; one runner tick                                 |
| `insert_stale_action` | `tool`, `args`, `started_offset_seconds`, `deadline_offset_seconds`, `save`    | none; writes an `in_flight` entry directly            |
| `rpc`                 | `method`, `params`                                                             | Protocol reply                                        |
| `check_schema`        | none                                                                           | `valid` and `invalid` lists of schema definitions     |
| `validate`            | `schema`                                                                       | `valid` and `invalid` lists of values                 |
| `register`            | none                                                                           | `accepted` and `refused` lists of `{name, schema}`    |

`recent_actions` reads the entries from the `actions_recent` tool however each engine shapes its data and
requires an `ok` outcome. `entries` must match the listed entries in any order, one to one. `insert_stale_action`
offsets are seconds relative to now and its `save` names the inserted entry's identifier. `rpc` adds the
modern protocol version to `params._meta`, so no initialization handshake is needed.

### Outcome envelope expectations

| Key                | Check                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------- |
| `status`, `tool`   | Equal                                                                                  |
| `action`           | The action state, or `null` for no `action`                                            |
| `approval_id`      | `true` if present, `false` if absent                                                   |
| `absent`           | Top-level keys that must be absent                                                     |
| `data`             | Subset match: listed object keys must match, arrays must match item by item            |
| `instants`         | Path to an instant (compared as a moment, whatever its offset or rendering), or `null` |
| `patterns`         | Path to a regular expression the whole string must match                               |
| `types`            | Path to a JSON type name; `number` also accepts integers                               |
| `message_contains` | Texts the message must contain                                                         |

Every returned `action` must be `{"id", "state"}` with 32 lowercase hexadecimal characters and a known state.
Scalars compare as JSON values: `true` never equals `1`, and `1.0` equals `1`. Paths are dot-separated object
keys and array indexes, such as `data.tasks.0.id`.

Protocol replies take `error_code` (the error's code) or, for a result, `is_error`, `structured_content` (whether
`structuredContent` is present), `outcome` (envelope expectations for the JSON text in the first content item),
`listed`, `not_listed`, `listed_count`, `all_listed_match` and `none_listed_match` (glob patterns).

### Values

`{"$saved": "name"}` stands for a value saved earlier in the case: `save` maps names to paths in the step's
result. `{"$engine": "absent_approval_id"}` is a well-formed approval identifier that names no request in that
engine. Neither form is substituted inside schemas or values of `check_schema`, `validate` and `register`.

## Divergences and known violations

A step on which the engines legitimately differ has `divergence` with a `topic` (the start of a row in the
contract's Known divergences table), a `reason`, and `python` and `native` expectations. Those keys replace the
same keys of `expect` for that engine; everything else in `expect` is still checked for both.

A case on which an engine does not yet meet a normative rule has `known_violation`, mapping the engine to the
reason. That runner reports the case as an expected failure and fails when the case starts passing, so the marker
is removed once the engine is fixed.

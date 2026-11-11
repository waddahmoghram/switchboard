# C++-first native Switchboard

This is a native C++20 engine, not a launcher for Python. C99 and Rust clients use the same engine
through its versioned C application binary interface (ABI). Policy, approval, validation, storage and
connector logic are not independently copied into three implementations.
Native requests and configuration use JavaScript Object Notation (JSON).

The existing Python package remains the fuller reference implementation. The native backend targets the
local-first connector families; it is **not a full port of the account and social connectors**. Unsupported
configuration and operations are rejected rather than forwarded to Python.

All programs and tests are authored but unrun. Compiler syntax/type or metadata checks are not a successful
link, passing test, provider sign-in, device compatibility check, or verified physical effect.

## Architecture

```text
C++ command-line/protocol host ----+
C99 client -> versioned C ABI ----+--> C++ validation / policy / approvals --> native adapters
Rust safe wrapper -> C ABI -------+                     |
                                                       +--> private SQLite state and events
```

The public [C++ interface](./include/switchboard/native.hpp) owns the runtime.
The [C header](./include/switchboard/switchboard.h) uses opaque handles, explicit byte lengths and
fixed-width status/origin values. The [Rust library](./clients/rust/src/lib.rs) owns those handles and
buffers safely; it neither imports Python nor implements another policy engine.

The Model Context Protocol (MCP) host uses standard input/output, with no listening port. Modern requests
use `2026-07-28`; the native legacy handshake selects `2025-11-25`. This is authored protocol support,
not client certification.

## Coverage

| Area                                                  | Native route                                | Important boundary                                                                                  |
| ----------------------------------------------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Policy and approvals                                  | C++ engine, shared by C/Rust                | Current schemas/policy rechecked; same-user callers are not sandboxed                               |
| Home Assistant                                        | [Home adapter](./docs/home.md)              | Light/climate/fan/media profiles; no native presets, speech, classified summaries or polling/events |
| WLED and Shelly                                       | [Direct device adapters](./docs/devices.md) | Fixed endpoints, reviewed scope and explicit observation limits                                     |
| Documents and tasks                                   | [Personal adapters](./docs/personal.md)     | Private bounded text/local task storage, not general file management or provider sync               |
| Backup/health/capacity observations                   | [Monitoring](./docs/monitoring.md)          | Read-only declared criteria; no backup execution or restore-integrity claim                         |
| Mail, calendars, contacts, finance, shopping          | Python only                                 | Not registered or silently delegated in the native engine                                           |
| Notifications, webhooks, Telegram, LinkedIn, WhatsApp | Python only                                 | No native account authorization or transport parity                                                 |
| Automation rules                                      | Python runner only                          | Native jobs queue events; native clients must inspect/handle them explicitly                        |

Per-adapter guides define exact tools and accepted configuration. A family being listed is not a promise
that every Python setting, model, action or protocol feature has native parity.

## Dependencies and style

The native engine uses libcurl, SQLite, OpenSSL's cryptographic library and nlohmann/json.
Dependencies are declared in [CMake](./CMakeLists.txt) and [the vcpkg manifest](./vcpkg.json).
They replace unsafe hand-written transport, cryptography and JSON parsing, not an aggregator subscription.
The Python package still has no third-party Python runtime dependencies.

Native code uses ordinary C++20 standard-library types, four-space Allman formatting, and
[its local formatter configuration](./.clang-format). This is a standalone personal project, not
firmware: no private firmware wrappers, panic macros, build functions or company ownership banner is imported.
No project license has been chosen; dependency licenses remain their respective owners'.

Linux and macOS are the target operating systems. Descriptor-relative private-file access is not a
Windows implementation. Compiler, library and operating-system combinations remain untested.

## Build on your own machine

These are **unexecuted instructions**, not commands run on the authoring host. Install a C++20 compiler,
C99 compiler, CMake 3.20+, and development packages for the four libraries above. On a suitable Ubuntu
personal machine, for example:

```bash
sudo apt install \
  g++ cmake libcurl4-openssl-dev libsqlite3-dev \
  nlohmann-json3-dev libssl-dev cargo
cmake -S native -B build/native \
  -DCMAKE_BUILD_TYPE=Debug \
  -DSWITCHBOARD_BUILD_TESTS=ON
cmake --build build/native
ctest --test-dir build/native --output-on-failure
```

Tests use synthetic data and fake transports, but inspect the suite before running it and block external
network access where practical. The build option defaults to off; creating test targets is not running them.
No automatic dependency download or provider connection is part of the CMake configuration.

For Rust, build the native library first, then make that library discoverable to the Rust compiler and
operating-system loader. A Linux example, from the repository root:

```bash
export RUSTFLAGS="-L native=$(pwd)/build/native"
export LD_LIBRARY_PATH="$(pwd)/build/native"
cargo build --manifest-path native/clients/rust/Cargo.toml
cargo test --manifest-path native/clients/rust/Cargo.toml
```

This replaces `LD_LIBRARY_PATH` for that shell; preserve any existing entries you need. macOS uses a
different loader configuration. No Rust build script invokes CMake, installs tools or launches Python.
The Rust package has no third-party Rust dependencies, but requires the C++ shared library at link/run time.

## First use without accounts

Use the [inert starter](./config.example.json). It enables no connectors and exposes only core metadata
tools. Native configuration uses JSON, not the Python configuration file parser.
Do not pass a Python configuration file or Python database to the native runtime.

Create a dedicated private state directory on your own machine first:

```bash
mkdir -p "$HOME/.local/share/switchboard-native"
chmod 700 "$HOME/.local/share/switchboard-native"
build/native/switchboard-native --config native/config.example.json catalog
build/native/switchboard-native --config native/config.example.json \
  call switchboard_status '{}'
build/native/switchboard-c native/config.example.json catalog
```

Then enable one reviewed adapter using its example, keep only the exposure you intend, and test it on your
own personal setup. Connector registration is offline; explicit reads/polls and future runner ticks can
contact configured endpoints or read selected files. Do not copy account tokens or real notes merely
because the tests use fakes.

## State and operational semantics

The native default directory is separate: `~/.local/share/switchboard-native/`, with
`native-state.sqlite3`. This is not a migration or compatibility layer for the Python database/audit file.
Do not point different versions at each other's state or assume approval identifiers/hashes are interchangeable.
Native approvals use 32 hexadecimal characters.

Read/draft normally allow, actions normally ask, and spending is always denied. Native per-tool policy
supports `decision` and `max_per_hour`; unsupported Python policy fields are errors. An exposure list
limits assistant-origin calls. Explicit human-origin calls are operator conveniences, not proof a person
is present. An assistant with the same user's unrestricted files or shell can bypass this gate.

Create `KILL_SWITCH` in the native data directory to block non-read calls. It does not stop reads, polling
or an already submitted request. Stop the process to stop background activity. The operator removes the
file to release the switch.

```bash
touch "$HOME/.local/share/switchboard-native/KILL_SWITCH"
build/native/switchboard-native --config native/config.example.json run --once
```

The runner is serial. Jobs have minimum intervals, not guaranteed maximum latency.
Native events have no automatic notification/rule delivery. Old event payloads are pruned on runner ticks;
deduplication keys can remain. Audit, approval and adapter data do not have one global deletion policy.
The audit chain can detect altered/interior records, not valid-tail deletion without an independent head.

An outer successful call means a handler returned a report. A pending approval is not execution;
accepted control is not observed state; observed software state is not independent physical verification.
Never blindly repeat an ambiguous external effect. Interrupted approvals can remain `running` and must
be reconciled manually.

See [C/Rust interface ownership and error semantics](./docs/bindings.md), the adapter guides above, and
the repository's [operating boundary](../docs/DEPLOYMENT.md#the-operating-boundary).

# Validation and packaging

This page describes how Switchboard is checked away from the authoring host: a continuous integration (CI)
workflow, a local script that runs the same stages on your own machine, CMake presets, sanitizer, fault and
fuzz targets, and pinned dependencies.

**Status: authored and unexecuted.** No workflow run, configure, build, link, test, fuzzing session, install
or Rust build of anything described here has been observed. The only checks performed while writing it were
static: see [Current status](#current-status). A stage that passes on your machine proves only what its row
in [Stages at a glance](#stages-at-a-glance) claims.

## Stages at a glance

| Stage        | Script stage | CI job                                 | Proves when it passes                                                                                           | Does not prove                                                             |
| ------------ | ------------ | -------------------------------------- | --------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| Python       | `python`     | `python` (3.11-3.13, Ubuntu and macOS) | Lint, formatting, static types, unit, contract, fault, seeded fuzz and local integration tests pass             | Behavior against real accounts, providers or devices                       |
| Contracts    | `contracts`  | `contracts`                            | Both engines produce the outcomes the shared fixtures in `contracts/cases` require                              | Agreement outside the fixtures, or features only one engine has            |
| Native       | `native`     | `native` (three compilers)             | The C++ engine, C client and every native test build with warnings as errors and pass                           | Compatibility with other compilers, libraries or operating systems         |
| Sanitizers   | `sanitizers` | `sanitizers`                           | No memory, undefined-behavior or leak report on the native tests, and no data race in the concurrency suites    | Absence of defects on paths the tests do not reach                         |
| Rust         | `rust`       | `rust`                                 | The Rust wrapper builds against the release library and its tests pass through the C interface                  | Behavior of other Rust toolchains or link setups                           |
| Fuzz smoke   | `fuzz`       | `fuzz`                                 | Seed corpora replay cleanly and a bounded run per target finds no crash or invariant violation                  | Robustness beyond the time spent; a short run is a smoke test, not a proof |
| Package      | `package`    | `package`                              | A release install provides the library, headers, CMake package and pkg-config file that two consumers can use   | Distribution packaging, signing, or system-wide installs                   |

The `native` job builds with GNU Compiler Collection (GCC) and Clang on Ubuntu, and Apple Clang on macOS.

## Running it on your machine

Run the stages from the repository root with your normal account; the script refuses to run as root, because
several fault tests rely on file permissions that root ignores. Every build, cache and scratch directory goes
under `build/`, which `.gitignore` already excludes. The first `uv sync` writes `uv.lock` in the repository
root; review it before committing.

```bash
scripts/ci/validate.sh             # every stage, in table order
scripts/ci/validate.sh python      # one stage
scripts/ci/validate.sh native rust # several stages, in the given order
FUZZ_SECONDS=600 scripts/ci/validate.sh fuzz
```

Prerequisites per stage:

| Stage        | Needs                                                                                                       |
| ------------ | ----------------------------------------------------------------------------------------------------------- |
| `python`     | `uv`, which installs Python 3.11+ and the pinned development tools                                          |
| `contracts`  | Everything for `python` and `native`                                                                        |
| `native`     | CMake 3.25+ (for presets version 6), a C++20 and C99 compiler, and libcurl, SQLite, nlohmann/json, OpenSSL  |
| `sanitizers` | `native`, plus the compiler's sanitizer runtimes (for Clang on Ubuntu: `libclang-rt-<major>-dev`)           |
| `rust`       | `native` and Cargo                                                                                          |
| `fuzz`       | The LLVM project's Clang (`clang`, `clang++`) with libFuzzer; Apple Clang does not ship libFuzzer           |
| `package`    | `native`; `pkg-config` and `cc` for the optional C consumer                                                 |

On Ubuntu 24.04, for example (unexecuted):

```bash
sudo apt-get install \
  cmake g++ clang libclang-rt-18-dev pkg-config \
  libcurl4-openssl-dev libsqlite3-dev nlohmann-json3-dev libssl-dev
```

Environment variables: `SWITCHBOARD_PYTHON` selects a Python version for `uv sync`; `FUZZ_SECONDS` (default
30) bounds each fuzz target; `SWITCHBOARD_SKIP_TSAN=1` skips the ThreadSanitizer half of `sanitizers`;
`CMAKE_BUILD_PARALLEL_LEVEL` defaults to the processor count. The CMake presets can also be used directly
from `native/`, which is where `cmake --preset` and `ctest --preset` look for `CMakePresets.json`:

```bash
cd native
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
cmake --workflow --preset asan-ubsan
```

## Python stage

`uv sync` installs the project and the `dev` dependency group, then Ruff checks lint and formatting, mypy checks
`src` and `tests`, and pytest runs twice: without and with the `integration` marker. Integration tests start
local servers and subprocesses only. The `contracts`, `fuzz` and `faults` markers are registered in
`pyproject.toml` and run in the first pytest pass; select one alone with, for example, `pytest -m faults`.

## Native presets and options

| Preset       | Build type     | Tests                  | Instrumentation                                            | Notes                                                    |
| ------------ | -------------- | ---------------------- | ---------------------------------------------------------- | -------------------------------------------------------- |
| `dev`        | Debug          | all                    | none; warnings are errors                                  | The default for CI and local development                 |
| `release`    | Release        | none                   | none                                                       | Used by the Rust and package stages and for installs     |
| `asan-ubsan` | Debug          | all                    | AddressSanitizer (ASan) and UndefinedBehaviorSanitizer     | Leak checks come with ASan on Linux                      |
| `tsan`       | Debug          | `concurrency` label    | ThreadSanitizer                                            | Builds every test, runs broker, recovery and lifecycle   |
| `fuzz`       | RelWithDebInfo | `fuzz-corpus` label    | libFuzzer with ASan and UndefinedBehaviorSanitizer (UBSan) | Pins `clang`/`clang++`; tests replay seeds only          |
| `coverage`   | Debug          | all                    | `--coverage` (gcov format)                                 | Produces `.gcda` files; reports are a separate step      |

Each preset builds in `build/native-<preset>`. Build, test and workflow presets share the configure preset's
name. Test presets print output on failure and treat "no tests" as an error.

| Option                           | Default | Effect                                                                                     |
| -------------------------------- | ------- | ------------------------------------------------------------------------------------------ |
| `SWITCHBOARD_BUILD_TESTS`        | `OFF`   | Builds the native test executables and registers them with CTest                           |
| `SWITCHBOARD_WARNINGS_AS_ERRORS` | `OFF`   | Adds `-Werror` to every Switchboard target (`-Wall -Wextra -Wpedantic` are always on)      |
| `SWITCHBOARD_SANITIZE`           | empty   | A list from `address`, `undefined`, `leak`, `thread`; `thread` excludes `address`, `leak`  |
| `SWITCHBOARD_BUILD_FUZZERS`      | `OFF`   | Builds `native-fuzz-<target>`; needs LLVM Clang and an `address` or `undefined` sanitizer  |
| `SWITCHBOARD_COVERAGE`           | `OFF`   | Compiles and links every target with `--coverage`                                          |

Fuzzer builds instrument the shared library itself; the sanitizer runtime supplies the coverage hooks to the
non-fuzzer programs that link it, which is why a sanitizer is required. A coverage report could then be made
with a tool such as gcovr, for example `gcovr --root . --filter native/src/ build/native-coverage` from the
repository root (a suggestion only; gcovr is not a project dependency).

CTest labels: `native` (every native test), `contracts`, `faults`, `concurrency` (broker, recovery and
lifecycle, the ThreadSanitizer selection) and `fuzz-corpus` (seed replays). `native-contracts` receives
`SWITCHBOARD_CONTRACTS_DIR`, the absolute path of `contracts/cases`, as a compile definition. Every test
executable links `Threads::Threads`.

## Sanitizers

The `asan-ubsan` preset runs every native test under ASan, LeakSanitizer (LSan, part of ASan on Linux) and
UBSan, with UBSan recovery disabled so the first report fails the test. The `tsan` preset builds every test
under ThreadSanitizer but runs only the `concurrency` label, where threads exist: the broker's sockets,
cancellation and deadline tests in `recovery`, and `lifecycle`. CI runs `asan-ubsan` with both
GCC and Clang, and `tsan` with Clang.

`alloc_faults` is not built with `thread` or standalone `leak`: those static runtimes define global
`operator new` as strong symbols, so replacing it would not link. ASan's definitions are weak and allow it.

Some kernels use more address-space layout randomization than older sanitizer runtimes expect. CI sets
`vm.mmap_rnd_bits=28` on its disposable runners; the local script only prints a warning with the command,
because changing a kernel setting is an administrator's decision.

## Fault injection

Native, in `native/tests/faults.cpp` (label `faults`):

- A fault transport makes one request fail as a transport error, byte limit, deadline, cancellation, server
  error status, malformed, oversized, deeply nested or non-UTF-8 body (UTF-8 is the 8-bit Unicode
  Transformation Format), or an unexpected exception. A read reports a failure without provider content and
  is not retried. A write is journaled as `uncertain` or `failed` as `contracts/README.md` classifies it, is
  not retried, blocks an identical repeat while uncertain, and runs again only after the operator reconciles
  it. The WLED adapter's real read path is checked against the same faults.
- Storage: a data directory under an unwritable parent, a read-only state file, a state file from a newer
  schema, a corrupt state file (which must be left byte-for-byte untouched) and a write lock held by another
  connection. Each must surface as a `switchboard::Error` or an error report through the C interface, and the
  runtime must work again with a verifiable audit chain once the fault is removed.

Native, in `native/tests/alloc_faults.cpp` (label `faults`): global `operator new` and `delete` are replaced
by `malloc` and `free` with a countdown. `sb_runtime_create`, `sb_runtime_destroy`, `sb_runtime_catalog`,
`sb_runtime_call`, `sb_runtime_call_outcome` and `sb_runtime_rpc` run with the failure placed at every
allocation (one-shot mode) and at sampled points with every later allocation failing too (sticky mode). Each
call must return one of the four documented status codes, publish a JavaScript Object Notation (JSON) report
exactly when its status promises one, keep exceptions inside the C boundary, and leave the runtime usable
with a verifiable audit chain. Every buffer and runtime is released, so LSan can attribute leaks on error
paths. `SWITCHBOARD_ALLOC_FAULT_STRIDE=<n>` samples every nth allocation to shorten a run. Exit code 77, which
CTest reports as skipped, means the replacement did not reach the shared library on that platform.

Python, in `tests/faults/` (marker `faults`):

| File                      | Faults                                                                                                               |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `test_storage_faults.py`  | `sqlite3.OperationalError` at each `Database.connect`; a persistent failure; newer, non-file and read-only databases |
| `test_audit_faults.py`    | `OSError` at each `AuditLog.append`, including before a handler runs; an unwritable audit log                        |
| `test_http_faults.py`     | `HttpError` and bad responses for a read probe and the WLED connector; transport failure during a write              |
| `test_protocol_faults.py` | Malformed, non-UTF-8, oversized, deeply nested and wrongly shaped protocol lines                                     |

The cases require well-formed replies or outcomes without the injected detail, no automatic retry, and, where
a runtime survives the fault, an audit chain that still verifies.

## Fuzzing

| Target      | Entry points                                         | Invariants checked beyond "no crash or sanitizer report"                                                                           |
| ----------- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `json`      | `parse_json`, `validate`, `hash`                     | Accepted JSON validates, hashes to 64 lowercase hexadecimal digits, and round-trips with its hash                                  |
| `schema`    | `check_schema`, `validate`                           | Checked schemas only yield `invalid`; results are deterministic and survive a round trip                                           |
| `timestamp` | `parse_timestamp`, `timestamp`                       | Accepted text renders and parses back; rendering rejects exactly the out-of-range instants                                         |
| `endpoint`  | `validate_endpoint`                                  | Accepted addresses are bounded, scheme-checked, credential-free, and monotonic in plain Hypertext Transfer Protocol (HTTP) consent |
| `digest`    | `validate_digest_challenge`                          | Accepted challenges carry the pinned realm, Secure Hash Algorithm 256 (SHA-256) and `qop=auth`                                     |
| `c_api`     | `sb_runtime_call`, `sb_runtime_rpc` (inert runtime)  | Documented status codes, terminated JSON reports, invalid origins rejected, only allowed tools run                                 |

The `schema` target compiles fuzzed patterns but validates fuzzed values only against pattern-free copies or
fixed engine-style schemas: schemas are trusted configuration, and the standard library's backtracking
regular expressions are not bounded-time for adversarial patterns. The `c_api` target creates a private
temporary state directory (mode 0700) in `LLVMFuzzerInitialize`, enables no connector, exposes and allows
only the core read tools, denies every other tool name by policy, and resets its state every 2048 inputs;
a crash may therefore depend on earlier inputs.

Seed corpora live in `native/fuzz/corpus/<target>/`, and `native/fuzz/json.dict` is a dictionary for the
JSON-shaped targets. The local script and CI pass a scratch corpus under `build/fuzz-scratch/` first, so
libFuzzer never writes into the committed seeds, and crash inputs go to `build/fuzz-artifacts/`. To reproduce
or extend, from the repository root:

```bash
build/native-fuzz/native-fuzz-json build/fuzz-artifacts/json-crash-<hash>
build/native-fuzz/native-fuzz-schema -max_total_time=3600 \
  -dict=native/fuzz/json.dict build/fuzz-scratch/schema native/fuzz/corpus/schema
```

Python fuzz tests in `tests/fuzz/` (marker `fuzz`) mutate seeds deterministically from a fixed seed:
`json_loads`, `check_schema`, `validate`, `load_rules`, rule matching and rendering, and the protocol request
handler. `SWITCHBOARD_FUZZ_SEED` and `SWITCHBOARD_FUZZ_ITERATIONS` change the seed and the per-test budget.

## Rust

The Rust stage builds `switchboard_native` with the `release` preset, then runs `cargo build` and `cargo test`
with `--locked`, the library directory on the link path and as a run-time search path of the Rust programs,
and `LD_LIBRARY_PATH` on Linux. Cargo builds under `build/rust-target`. The tests include
`tests/lifecycle.rs` and `tests/outcomes.rs`.

## Packaging and install

- The shared library is versioned `0.1.0` with shared object name (SONAME) version 1, which tracks
  `SB_ABI_VERSION` in `switchboard.h`, the C application binary interface (ABI) version. The C++ interface in
  `native.hpp` has no stable ABI: rebuild C++ embedders against each release.
- Symbols keep the default visibility: `native.hpp` has no export macros, and `native/tests/broker.cpp` links
  internal broker functions from `src/broker_internal.hpp` through the shared library.
- `cmake --install` installs the library, `switchboard-native`, `switchboard-c`, the headers, an exported target
  `switchboard::native` (also available as an alias inside the build), `SwitchboardNativeConfig.cmake` (which
  finds nlohmann_json, the only public dependency), a version file with same-minor-version compatibility, and
  `switchboard-native.pc`, whose paths are relative to its own location when the install directories are
  relative.
- Installed programs find the library through a relative run-time search path, so an install prefix can move.

```cmake
find_package(SwitchboardNative 0.1 REQUIRED CONFIG)
target_link_libraries(my_program PRIVATE switchboard::native)
```

The `package` stage installs a release build into `build/package-install`, then builds and runs a CMake
consumer (`find_package`, C and C++ headers) and, when available, a C99 consumer through `pkg-config`. Both
call only version queries: no runtime, state or network.

## Dependency pins

| Dependency                       | Pin                                                                                     |
| -------------------------------- | --------------------------------------------------------------------------------------- |
| Python development tools         | `pytest==8.4.2`, `ruff==0.15.0`, `mypy==1.18.2` in the `dev` group; no runtime packages |
| uv in CI                         | `0.13.7`                                                                                |
| vcpkg `builtin-baseline`         | `9eec002df1cd109f8f104cd966a763daf9751489`, release tag `2026.10.16`                    |
| Rust lockfile                    | `Cargo.lock` version 3, the single dependency-free package `switchboard-native` 0.1.0   |
| `actions/checkout`               | `3d3c42e5aac5ba805825da76410c181273ba90b1` (`v7.0.1`)                                   |
| `astral-sh/setup-uv`             | `68559e99cc807c1eb7e9238fd43a15bba460eec2` (`v10.3.3`)                                  |
| `actions/upload-artifact`        | `cf430e030ddbb5b0abf93d22962f4752f3646cd9` (`v7.0.2`)                                   |

The action and vcpkg identifiers are full commit Secure Hash Algorithm (SHA) values, resolved on 2026-11-11
through the GitHub application programming interface: each action tag is a lightweight tag on that
commit, and the annotated vcpkg tag `2026.10.16` (tagged 2026-10-20) points to a commit dated 2026-10-17. At
that baseline the ports are curl 8.23.0, sqlite3 3.53.4 (port version 2), nlohmann-json 3.12.0 (port version
2) and openssl 3.6.5 (port version 2), read from each port's manifest at that commit. No vcpkg install was run.

`Cargo.lock` is hand-authored in Cargo's version 3 format, which matches the package's `rust-version` of 1.63;
`--locked` fails if Cargo would change it. Not pinned: transitive Python development dependencies (no
`uv.lock` is committed yet), the runner images behind `ubuntu-24.04` and `macos-15`, Ubuntu and Homebrew
packages, CMake, and the runners' Rust toolchain.

## Workflow security notes

- The workflow token is read-only (`permissions: contents: read`); no job uses secrets or deploys anything.
- It runs on `push`, `pull_request` and manual dispatch, never `pull_request_target`, so code from a fork runs
  without secrets and with a read-only token.
- Checkout does not persist credentials, every action is pinned to a full commit SHA, and the uv cache is off,
  so one run cannot seed another run's tools.
- The only uploaded artifact is the fuzzing crash inputs of a failed run, kept for 14 days.
- `sudo` is used only on disposable runners, for distribution packages and the sanitizer `sysctl` setting.
- Concurrency groups cancel superseded pull request runs; a running push build is never cancelled.
- Pinned actions do not update themselves: review new releases and their SHAs deliberately (for example with
  Dependabot, which is not configured here).

## Review notes from authoring

Static reading while writing these tests found the following. Each has since been fixed in the source and is
covered by an authored test; none of the fixes has been run.

- `hash()` and `identifier()` in the native engine built text with `std::ostringstream`, and timestamps were
  rendered the same way. The standard libraries catch an allocation failure during formatted output and only
  set the stream's error state, so a shortened digest, identifier, or timestamp could have been returned
  silently. They now build strings directly or with `strftime`, so an allocation failure throws.
  `alloc_faults` ends with an audit-chain check that would expose a shortened audit digest.
- `serve_stdio` caught only `ValueError` and `UnicodeDecodeError` from `json_loads`, so a deeply nested
  protocol line raised `RecursionError` and ended the Python session. It now answers with a parse error and
  keeps the session; `test_protocol_faults.py` checks that.
- `rules.lookup` accepted path segments that pass `str.isdigit` but not `int`, such as a superscript digit.
  List indexes must now be ASCII digits.
- `check_schema` and `load_rules` converted only `re.error`, but `re.compile` can also raise `OverflowError`
  for huge repetition counts or `RecursionError` for very deep nesting. Both now become the usual
  configuration or schema error. The fuzz generators still avoid such patterns, because patterns come from
  trusted code and configuration.

## Current status

Authored, unexecuted. Static checks on the authoring host, all passing:

- Ruff 0.15.0 lint and format check on `src` and `tests`; mypy 1.18.2 on `src` and `tests` (Python 3.12).
- `-fsyntax-only` with GCC 11 and `-Wall -Wextra -Wpedantic -Werror` on the fuzz targets and fault tests;
  `clang-format` 14 with `native/.clang-format`.
- The workflow parsed as YAML Ain't Markup Language (YAML) data, the presets and manifests as JSON, and
  `Cargo.lock` and `pyproject.toml` as Tom's Obvious, Minimal Language (TOML); actionlint 1.7.12 on the
  workflow; `bash -n` and ShellCheck 0.11.0 on the script; cmake-lint on `CMakeLists.txt` and the package
  configuration template.

Unknowns that only a run can settle: Clang, Apple Clang and GCC 13 or newer diagnostics under `-Werror`;
whether every sanitizer, fuzz target and fault test passes; runtimes of `alloc_faults` and the sanitizer jobs;
and runner-image changes. Every source and test file the CMake lists name exists; none is optional, so a
missing file fails configuration instead of silently shrinking the suite.

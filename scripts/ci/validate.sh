#!/usr/bin/env bash
# Run Switchboard's validation stages on your own trusted machine. Authored and unexecuted.
#
# Usage: scripts/ci/validate.sh [STAGE...]
#   python      uv sync; Ruff lint and format check; mypy; pytest without, then with, the integration marker
#   contracts   the shared fixtures in contracts/cases through the Python and the native engine
#   native      dev preset: configure, build, and run every native test (warnings are errors)
#   sanitizers  asan-ubsan preset with every native test, then tsan preset with the concurrency label
#   rust        release native library, then cargo build and test with that library's path
#   fuzz        fuzz preset (LLVM Clang): replay the seed corpora, then FUZZ_SECONDS per target
#   package     release build, scratch install, then CMake and pkg-config consumers of the install
#   all         every stage above, in that order (the default)
#
# Environment: SWITCHBOARD_PYTHON (a Python version for uv), FUZZ_SECONDS (default 30),
# SWITCHBOARD_SKIP_TSAN=1, CMAKE_BUILD_PARALLEL_LEVEL (default: the processor count).
# Build output, caches, and scratch files stay under build/; the first uv sync also writes uv.lock.
# No stage contacts an account or a device; uv downloads Python and packages from their public indexes.
set -euo pipefail

if [[ "$(id -u)" -eq 0 ]]; then
    echo "ERROR: refusing to run as root; use your normal account so permission checks mean something" >&2
    exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
build_dir="$repo_root/build"
native_dir="$repo_root/native"
current_stage="setup"
python_ready=0

if [[ -z "${CMAKE_BUILD_PARALLEL_LEVEL:-}" ]]; then
    CMAKE_BUILD_PARALLEL_LEVEL="$(getconf _NPROCESSORS_ONLN 2> /dev/null || echo 2)"
fi
export CMAKE_BUILD_PARALLEL_LEVEL

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "${BASH_SOURCE[0]}"
}

on_exit() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        printf '\nERROR: stage "%s" failed with exit status %s\n' "$current_stage" "$status" >&2
    fi
}
trap on_exit EXIT

stage() {
    printf '\n==> %s\n' "$*"
}

run() {
    printf '+ %s\n' "$*"
    "$@"
}

require() {
    local tool
    for tool in "$@"; do
        if ! command -v "$tool" > /dev/null 2>&1; then
            echo "ERROR: $tool is required for the $current_stage stage" >&2
            exit 1
        fi
    done
}

# CMake and CTest read CMakePresets.json from the working directory.
preset() {
    (cd "$native_dir" && run "$@")
}

in_repo() {
    (cd "$repo_root" && run "$@")
}

check_randomization() {
    local bits
    if [[ -r /proc/sys/vm/mmap_rnd_bits ]]; then
        bits="$(cat /proc/sys/vm/mmap_rnd_bits)"
        if ((bits > 28)); then
            echo "WARNING: vm.mmap_rnd_bits is $bits; some sanitizer runtimes fail above 28." >&2
            echo "         If a sanitizer reports an unexpected memory mapping, an administrator can run" >&2
            echo "         'sysctl -w vm.mmap_rnd_bits=28' (it reverts at reboot); this script never does." >&2
        fi
    fi
}

python_sync() {
    if [[ $python_ready -eq 1 ]]; then
        return
    fi
    require uv
    export UV_PROJECT_ENVIRONMENT="$build_dir/python-venv"
    export RUFF_CACHE_DIR="$build_dir/ruff-cache"
    export MYPY_CACHE_DIR="$build_dir/mypy-cache"
    export PYTHONPYCACHEPREFIX="$build_dir/pycache"
    stage "python: install the project and the pinned development tools"
    if [[ -n "${SWITCHBOARD_PYTHON:-}" ]]; then
        in_repo uv sync --python "$SWITCHBOARD_PYTHON"
    else
        in_repo uv sync
    fi
    python_ready=1
}

python_stage() {
    python_sync
    stage "python: Ruff lint and format check"
    in_repo uv run --no-sync ruff check src tests
    in_repo uv run --no-sync ruff format --check src tests
    stage "python: mypy"
    in_repo uv run --no-sync mypy --config-file pyproject.toml src tests
    stage "python: unit, contract, fault and fuzz tests"
    in_repo uv run --no-sync pytest -o cache_dir="$build_dir/pytest-cache" -m "not integration"
    stage "python: integration tests (local servers and subprocesses only)"
    in_repo uv run --no-sync pytest -o cache_dir="$build_dir/pytest-cache" -m integration
}

contracts_stage() {
    python_sync
    require cmake ctest
    stage "contracts: Python engine against contracts/cases"
    in_repo uv run --no-sync pytest -o cache_dir="$build_dir/pytest-cache" tests/contracts
    stage "contracts: native engine against contracts/cases"
    preset cmake --preset dev
    preset cmake --build --preset dev --target native-test-contracts
    preset ctest --preset dev --label-regex contracts
}

native_stage() {
    require cmake ctest
    stage "native: configure, build and test (dev preset)"
    preset cmake --preset dev
    preset cmake --build --preset dev
    preset ctest --preset dev
}

sanitizers_stage() {
    require cmake ctest
    check_randomization
    stage "sanitizers: AddressSanitizer and UndefinedBehaviorSanitizer, every native test"
    preset cmake --preset asan-ubsan
    preset cmake --build --preset asan-ubsan
    preset ctest --preset asan-ubsan
    if [[ "${SWITCHBOARD_SKIP_TSAN:-0}" == 1 ]]; then
        stage "sanitizers: ThreadSanitizer skipped (SWITCHBOARD_SKIP_TSAN=1)"
        return
    fi
    stage "sanitizers: ThreadSanitizer, concurrency label"
    preset cmake --preset tsan
    preset cmake --build --preset tsan
    preset ctest --preset tsan
}

rust_stage() {
    require cmake cargo
    stage "rust: release build of the native library"
    preset cmake --preset release
    preset cmake --build --preset release --target switchboard_native
    stage "rust: cargo build and test against build/native-release"
    local library="$build_dir/native-release"
    export CARGO_TARGET_DIR="$build_dir/rust-target"
    # Cargo splits CARGO_ENCODED_RUSTFLAGS on 0x1f, so a library path with spaces stays one argument.
    CARGO_ENCODED_RUSTFLAGS="$(printf -- '-L\037native=%s\037-Clink-arg=-Wl,-rpath,%s' "$library" "$library")"
    export CARGO_ENCODED_RUSTFLAGS
    if [[ "$(uname -s)" == Linux ]]; then
        export LD_LIBRARY_PATH="$library${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
    in_repo cargo build --locked --all-targets --manifest-path native/clients/rust/Cargo.toml
    in_repo cargo test --locked --manifest-path native/clients/rust/Cargo.toml
}

fuzz_stage() {
    require cmake ctest clang clang++
    check_randomization
    stage "fuzz: build the libFuzzer targets (fuzz preset)"
    preset cmake --preset fuzz
    preset cmake --build --preset fuzz
    stage "fuzz: replay the committed seed corpora"
    preset ctest --preset fuzz
    local seconds="${FUZZ_SECONDS:-30}"
    local target scratch dictionary
    mkdir -p "$build_dir/fuzz-artifacts"
    for target in json schema timestamp endpoint digest c_api; do
        stage "fuzz: $target for ${seconds}s"
        scratch="$build_dir/fuzz-scratch/$target"
        mkdir -p "$scratch"
        dictionary=""
        case "$target" in
            json | schema | c_api) dictionary="-dict=$native_dir/fuzz/json.dict" ;;
            *) ;;
        esac
        # The scratch corpus comes first, so new inputs never modify the committed seeds.
        run "$build_dir/native-fuzz/native-fuzz-$target" \
            -max_total_time="$seconds" \
            -timeout=10 \
            -rss_limit_mb=2048 \
            -print_final_stats=1 \
            -artifact_prefix="$build_dir/fuzz-artifacts/$target-" \
            ${dictionary:+"$dictionary"} \
            "$scratch" "$native_dir/fuzz/corpus/$target"
    done
}

write_consumer() {
    local source="$1"
    mkdir -p "$source"
    cat > "$source/CMakeLists.txt" << 'EOF'
cmake_minimum_required(VERSION 3.20)
project(SwitchboardConsumer LANGUAGES CXX)
find_package(SwitchboardNative 0.1 REQUIRED CONFIG)
add_executable(switchboard-consumer main.cpp)
target_link_libraries(switchboard-consumer PRIVATE switchboard::native)
set_target_properties(switchboard-consumer PROPERTIES CXX_STANDARD 20 CXX_STANDARD_REQUIRED ON)
EOF
    cat > "$source/main.cpp" << 'EOF'
#include <switchboard/native.hpp>
#include <switchboard/switchboard.h>

#include <cstdio>

// Calls only version queries: no runtime is created, no state is written and nothing is contacted.
int main()
{
    static_assert(switchboard::MAX_REQUEST_BYTES == SB_MAX_INPUT_BYTES);
    if (sb_abi_version() != SB_ABI_VERSION)
    {
        std::fputs("ERROR: the installed library and header disagree on the ABI version\n", stderr);
        return 1;
    }
    std::printf("switchboard-native ABI %u revision %u\n", sb_abi_version(), sb_abi_revision());
    return 0;
}
EOF
    cat > "$source/main.c" << 'EOF'
#include <switchboard/switchboard.h>

#include <stdio.h>

int main(void)
{
    if (sb_abi_version() != SB_ABI_VERSION)
    {
        fputs("ERROR: the installed library and header disagree on the ABI version\n", stderr);
        return 1;
    }
    printf("switchboard-native C ABI %u\n", (unsigned int)sb_abi_version());
    return 0;
}
EOF
}

package_stage() {
    require cmake
    stage "package: release build and scratch install"
    preset cmake --preset release
    preset cmake --build --preset release
    local prefix="$build_dir/package-install"
    local consumer="$build_dir/package-consumer"
    rm -rf -- "$prefix" "$consumer"
    run cmake --install "$build_dir/native-release" --prefix "$prefix"
    local config
    config="$(find "$prefix" -name SwitchboardNativeConfig.cmake -print -quit)"
    if [[ -z "$config" ]]; then
        echo "ERROR: the install has no SwitchboardNativeConfig.cmake" >&2
        exit 1
    fi
    stage "package: CMake consumer through find_package(SwitchboardNative)"
    write_consumer "$consumer/src"
    run cmake -S "$consumer/src" -B "$consumer/cmake" -DCMAKE_PREFIX_PATH="$prefix"
    run cmake --build "$consumer/cmake"
    run "$consumer/cmake/switchboard-consumer"
    stage "package: pkg-config consumer of the C interface"
    if ! command -v pkg-config > /dev/null 2>&1 || ! command -v cc > /dev/null 2>&1; then
        echo "SKIP: pkg-config or cc is not installed"
        return
    fi
    local metadata
    metadata="$(find "$prefix" -name switchboard-native.pc -print -quit)"
    if [[ -z "$metadata" ]]; then
        echo "ERROR: the install has no switchboard-native.pc" >&2
        exit 1
    fi
    export PKG_CONFIG_PATH
    PKG_CONFIG_PATH="$(dirname -- "$metadata")"
    local -a cflags libs
    read -r -a cflags <<< "$(pkg-config --cflags switchboard-native)"
    read -r -a libs <<< "$(pkg-config --libs switchboard-native)"
    local libdir
    libdir="$(pkg-config --variable=libdir switchboard-native)"
    run cc -std=c99 -Wall -Wextra -Wpedantic -Werror ${cflags[@]+"${cflags[@]}"} "$consumer/src/main.c" \
        -o "$consumer/consumer-c" ${libs[@]+"${libs[@]}"} "-Wl,-rpath,$libdir"
    run "$consumer/consumer-c"
}

main() {
    if [[ $# -eq 0 ]]; then
        set -- all
    fi
    local name started
    for name in "$@"; do
        current_stage="$name"
        case "$name" in
            python | contracts | native | sanitizers | rust | fuzz | package)
                started=$SECONDS
                "${name}_stage"
                printf '\n==> %s passed in %ss\n' "$name" "$((SECONDS - started))"
                ;;
            all)
                main python contracts native sanitizers rust fuzz package
                ;;
            -h | --help | help)
                usage
                ;;
            *)
                usage >&2
                exit 2
                ;;
        esac
    done
}

main "$@"

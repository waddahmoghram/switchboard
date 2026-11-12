// libFuzzer target: the C interface (sb_runtime_call and sb_runtime_rpc) against an inert runtime.
// Authored and unrun.
//
// LLVMFuzzerInitialize creates a private temporary state directory (mode 0700, from mkdtemp) and an inert
// runtime: no connectors, only the core read tools are exposed and allowed, and every other tool name is
// denied by policy, so no input can reach an adapter, a device or the network. The state is reset every
// RESET_INTERVAL inputs to bound database growth; a crash may therefore depend on earlier inputs.
//
// Input: one selector byte, then the request bytes. Odd selectors call sb_runtime_rpc. Even selectors call
// sb_runtime_call with origin (selector >> 1) % 4, where 3 is deliberately invalid.
#include "common.hpp"

#include "switchboard/switchboard.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>
#include <vector>

#include <unistd.h>

using switchboard::Json;
using switchboard::fuzz::check;

namespace
{
constexpr std::uint64_t RESET_INTERVAL = 2048;
const std::array<const char *, 7> READ_TOOLS = {"switchboard_status", "approvals_pending", "approvals_status", "events_recent",
                                                "audit_verify",       "actions_recent",    "jobs_status"};

sb_runtime *runtime = nullptr;
std::filesystem::path directory;
std::uint64_t executions = 0;

Json configuration()
{
    Json policy = {{"*", {{"decision", "deny"}}}};
    Json exposed = Json::array();
    for (const auto *name : READ_TOOLS)
    {
        policy[name] = {{"decision", "allow"}};
        exposed.push_back(name);
    }
    return {{"switchboard", {{"connectors", Json::array()}, {"data_dir", directory.string()}}},
            {"connectors", Json::object()},
            {"policy", {{"tools", policy}}},
            {"mcp", {{"expose", exposed}}}};
}

void create()
{
    const auto text = configuration().dump();
    sb_buffer error{nullptr, 0};
    const auto status = sb_runtime_create(reinterpret_cast<const std::uint8_t *>(text.data()), text.size(), &runtime, &error);
    if (status != SB_OK || runtime == nullptr)
    {
        std::fprintf(stderr, "ERROR: the inert fuzzing runtime could not be created (status %d): %.*s\n", static_cast<int>(status),
                     static_cast<int>(error.length), error.data == nullptr ? "" : reinterpret_cast<const char *>(error.data));
        sb_buffer_free(&error);
        std::abort();
    }
}

void remove_state() noexcept
{
    for (const auto *name : {"native-state.sqlite3", "native-state.sqlite3-wal", "native-state.sqlite3-shm", "native-state.sqlite3-journal"})
    {
        std::error_code ignored;
        std::filesystem::remove(directory / name, ignored);
    }
}

void cleanup()
{
    sb_runtime_destroy(runtime);
    runtime = nullptr;
    std::error_code error;
    std::filesystem::remove_all(directory, error);
    if (error)
    {
        std::fputs("ERROR: the fuzzing state directory could not be removed\n", stderr);
    }
}

bool allowed_tool(const Json &name)
{
    return name.is_string() && std::any_of(READ_TOOLS.begin(), READ_TOOLS.end(),
                                           [&](const char *tool)
                                           {
                                               return name == tool;
                                           });
}

void check_report(const sb_status status, const sb_buffer &output, const bool rpc, const sb_origin origin)
{
    check(status == SB_OK || status == SB_INVALID_ARGUMENT || status == SB_RUNTIME_ERROR, "unexpected C status code");
    check(rpc || (origin >= SB_ASSISTANT && origin <= SB_HUMAN) || status == SB_INVALID_ARGUMENT, "an invalid origin was not rejected");
    check(output.data != nullptr && output.length > 0 && output.data[output.length] == 0, "a C result is missing or not terminated");
    const std::string text(reinterpret_cast<const char *>(output.data), output.length);
    check(Json::accept(text), "a C result is not JSON");
    const auto report = Json::parse(text);
    const auto field = [&report](const char *key)
    {
        return report.is_object() && report.contains(key) ? report.at(key) : Json();
    };
    if (status != SB_OK)
    {
        check(field("status") == "error" && field("code").is_string(), "a C failure lacks an error report");
        return;
    }
    if (rpc)
    {
        check(report.is_null() || (field("jsonrpc") == "2.0" && report.contains("result") != report.contains("error")), "a protocol reply is malformed");
        return;
    }
    check(field("status").is_string() && field("tool").is_string(), "a call outcome lacks its envelope");
    const auto outcome = field("status");
    check(outcome == "ok" || outcome == "pending" || outcome == "denied" || outcome == "invalid" || outcome == "error", "unknown outcome status");
    check(outcome != "ok" || allowed_tool(field("tool")), "a tool outside the inert allowlist ran");
}
}  // namespace

extern "C" int LLVMFuzzerInitialize(int *, char ***)
{
    const char *base = std::getenv("TMPDIR");
    std::string pattern = (base != nullptr && base[0] == '/' ? std::string(base) : std::string("/tmp")) + "/switchboard-fuzz-XXXXXX";
    std::vector<char> buffer(pattern.begin(), pattern.end());
    buffer.push_back('\0');
    if (mkdtemp(buffer.data()) == nullptr)
    {
        std::perror("ERROR: mkdtemp for the fuzzing state directory");
        std::abort();
    }
    directory = buffer.data();
    create();
    if (std::atexit(cleanup) != 0)
    {
        std::fputs("ERROR: the fuzzing cleanup handler could not be registered\n", stderr);
    }
    return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    if (size == 0)
    {
        return 0;
    }
    if (++executions % RESET_INTERVAL == 0)
    {
        sb_runtime_destroy(runtime);
        runtime = nullptr;
        remove_state();
        create();
    }
    const auto selector = data[0];
    const bool rpc = (selector & 1U) != 0;
    const auto origin = static_cast<sb_origin>((selector >> 1U) % 4U);
    sb_buffer output{nullptr, 0};
    const auto status = rpc ? sb_runtime_rpc(runtime, data + 1, size - 1, &output) : sb_runtime_call(runtime, data + 1, size - 1, origin, &output);
    check_report(status, output, rpc, origin);
    sb_buffer_free(&output);
    check(output.data == nullptr && output.length == 0, "sb_buffer_free did not reset the buffer");
    return 0;
}

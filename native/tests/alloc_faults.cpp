// Allocation-failure injection at the C interface. Authored and unrun.
//
// Global operator new and delete are replaced by malloc and free wrappers with a countdown: after N successful
// allocations the next one fails (one-shot mode) or every later one fails (sticky mode). Each C entry point runs
// with N = 0, stride, 2 * stride, ... until it completes without reaching the injected failure. Every attempt must
// return a documented status code, publish a JSON report exactly when its status promises one, keep exceptions
// inside the C boundary and leave the runtime usable with a verifiable audit chain. Every buffer and runtime is
// released, so LeakSanitizer (on with AddressSanitizer on Linux) can attribute leaks on error paths.
//
// SWITCHBOARD_ALLOC_FAULT_STRIDE (a positive integer, default 1) samples every Nth allocation in one-shot mode;
// sticky mode always samples every 13th stride. Exit code 77 means the replacement did not reach the library.
//
// Allocations whose failure no caller could survive are never failed. nlohmann::json's noexcept destructor moves the
// children of an array or object onto a heap-allocated stack in json_value::destroy, so a failure there terminates
// the process. libc++'s basic_regex::__parse deletes its end state twice when the shared_ptr control block of its
// start state cannot be allocated. The failure moves to the next allocation outside those frames, which are found by
// their mangled names in a backtrace; that needs the unoptimized Debug build in which this test runs.
#include "support.hpp"
#include "switchboard/switchboard.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <new>
#include <stdexcept>
#include <string>
#include <system_error>

#include <dlfcn.h>
#include <execinfo.h>
#include <sys/stat.h>
#include <unistd.h>

namespace
{
// -1 is disarmed; otherwise the number of allocations that may still succeed before one fails.
std::atomic<std::int64_t> remaining{-1};
std::atomic<bool> sticky{false};
std::atomic<bool> triggered{false};
thread_local bool inspecting = false;

// Fragments of the mangled names of nlohmann::json's json_value::destroy and libc++'s basic_regex<char>::__parse.
constexpr const char *UNRECOVERABLE_FRAMES[] = {"10json_value7destroy", "11basic_regexIcNS_12regex_traitsIcEEE7__parseI"};

bool inside_unrecoverable_frame() noexcept
{
    constexpr int DEPTH = 64;
    void *frames[DEPTH];
    inspecting = true;
    const int depth = backtrace(frames, DEPTH);
    bool found = false;
    for (int index = 0; index < depth && !found; ++index)
    {
        Dl_info info{};
        if (dladdr(frames[index], &info) == 0 || info.dli_sname == nullptr)
        {
            continue;
        }
        for (const auto *fragment : UNRECOVERABLE_FRAMES)
        {
            found = found || std::strstr(info.dli_sname, fragment) != nullptr;
        }
    }
    inspecting = false;
    return found;
}

bool inject() noexcept
{
    if (inspecting)
    {
        return false;
    }
    auto current = remaining.load(std::memory_order_relaxed);
    while (current > 0)
    {
        if (remaining.compare_exchange_weak(current, current - 1, std::memory_order_relaxed))
        {
            return false;
        }
    }
    if (current < 0 || inside_unrecoverable_frame())
    {
        return false;
    }
    triggered.store(true, std::memory_order_relaxed);
    if (!sticky.load(std::memory_order_relaxed))
    {
        remaining.store(-1, std::memory_order_relaxed);
    }
    return true;
}

void *allocate(const std::size_t size) noexcept
{
    return inject() ? nullptr : std::malloc(size == 0 ? 1 : size);
}

void *allocate_aligned(const std::size_t size, const std::align_val_t alignment) noexcept
{
    if (inject())
    {
        return nullptr;
    }
    const auto bytes = static_cast<std::size_t>(alignment);
    void *memory = nullptr;
    return posix_memalign(&memory, bytes < sizeof(void *) ? sizeof(void *) : bytes, size == 0 ? 1 : size) == 0 ? memory : nullptr;
}

void *allocate_or_throw(const std::size_t size)
{
    if (void *memory = allocate(size))
    {
        return memory;
    }
    throw std::bad_alloc();
}

void *allocate_aligned_or_throw(const std::size_t size, const std::align_val_t alignment)
{
    if (void *memory = allocate_aligned(size, alignment))
    {
        return memory;
    }
    throw std::bad_alloc();
}
}  // namespace

void *operator new(const std::size_t size)
{
    return allocate_or_throw(size);
}

void *operator new[](const std::size_t size)
{
    return allocate_or_throw(size);
}

void *operator new(const std::size_t size, const std::nothrow_t &) noexcept
{
    return allocate(size);
}

void *operator new[](const std::size_t size, const std::nothrow_t &) noexcept
{
    return allocate(size);
}

void *operator new(const std::size_t size, const std::align_val_t alignment)
{
    return allocate_aligned_or_throw(size, alignment);
}

void *operator new[](const std::size_t size, const std::align_val_t alignment)
{
    return allocate_aligned_or_throw(size, alignment);
}

void *operator new(const std::size_t size, const std::align_val_t alignment, const std::nothrow_t &) noexcept
{
    return allocate_aligned(size, alignment);
}

void *operator new[](const std::size_t size, const std::align_val_t alignment, const std::nothrow_t &) noexcept
{
    return allocate_aligned(size, alignment);
}

void operator delete(void *memory) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory) noexcept
{
    std::free(memory);
}

void operator delete(void *memory, std::size_t) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory, std::size_t) noexcept
{
    std::free(memory);
}

void operator delete(void *memory, const std::nothrow_t &) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory, const std::nothrow_t &) noexcept
{
    std::free(memory);
}

void operator delete(void *memory, std::align_val_t) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory, std::align_val_t) noexcept
{
    std::free(memory);
}

void operator delete(void *memory, std::size_t, std::align_val_t) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory, std::size_t, std::align_val_t) noexcept
{
    std::free(memory);
}

void operator delete(void *memory, std::align_val_t, const std::nothrow_t &) noexcept
{
    std::free(memory);
}

void operator delete[](void *memory, std::align_val_t, const std::nothrow_t &) noexcept
{
    std::free(memory);
}

namespace
{
using switchboard::Json;
using switchboard::test::expect;

constexpr std::int64_t LIMIT = 2000000;
constexpr std::int64_t STICKY_FACTOR = 13;
const std::string CALL = R"({"tool":"switchboard_status","arguments":{}})";
const std::string RPC = R"({"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"switchboard_status","arguments":{},)"
                        R"("_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}})";

// A private state directory per attempt, created and removed while injection is disarmed.
class Scratch
{

public:
    Scratch()
    {
        static std::uint64_t sequence = 0;
        const auto tick = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::temp_directory_path() /
               ("switchboard-alloc-faults-" + std::to_string(getpid()) + "-" + std::to_string(tick) + "-" + std::to_string(sequence++));
        if (!std::filesystem::create_directory(path) || chmod(path.c_str(), 0700) != 0)
        {
            throw std::runtime_error("cannot create a private allocation-fault directory");
        }
    }

    ~Scratch()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: allocation-fault directory cleanup failed\n";
        }
    }

    Scratch(const Scratch &) = delete;
    Scratch &operator=(const Scratch &) = delete;
    std::filesystem::path path;
};

std::string inert(const Scratch &directory)
{
    return Json{{"switchboard", {{"connectors", Json::array()}, {"data_dir", directory.path.string()}}},
                {"connectors", Json::object()},
                {"policy", {{"tools", Json::object()}}},
                {"mcp", {{"expose", {"*"}}}}}
        .dump();
}

const std::uint8_t *bytes(const std::string &text)
{
    return reinterpret_cast<const std::uint8_t *>(text.data());
}

// Runs one C call with injection armed. The status is returned; an escaping exception fails the test.
template <typename Function> sb_status armed(const std::int64_t after, const bool sticky_mode, Function function)
{
    triggered.store(false, std::memory_order_relaxed);
    sticky.store(sticky_mode, std::memory_order_relaxed);
    remaining.store(after, std::memory_order_relaxed);
    sb_status status = SB_OK;
    bool escaped = false;
    try
    {
        status = function();
    }
    catch (...)
    {
        escaped = true;
    }
    remaining.store(-1, std::memory_order_relaxed);
    expect(!escaped, "an exception crossed the C boundary");
    return status;
}

bool injected()
{
    return triggered.load(std::memory_order_relaxed);
}

// Checks the status and report contract, then releases the buffer.
void check_result(const sb_status status, sb_buffer &output, const bool report_on_success, const std::string &where)
{
    expect(status == SB_OK || status == SB_INVALID_ARGUMENT || status == SB_RUNTIME_ERROR || status == SB_ALLOCATION_ERROR,
           where + ": undocumented status code");
    if (status == SB_ALLOCATION_ERROR || (status == SB_OK && !report_on_success))
    {
        expect(output.data == nullptr && output.length == 0, where + ": a buffer was published without a report");
        return;
    }
    expect(output.data != nullptr && output.length > 0 && output.data[output.length] == 0, where + ": the report is missing or unterminated");
    const std::string text(reinterpret_cast<const char *>(output.data), output.length);
    sb_buffer_free(&output);
    expect(output.data == nullptr && output.length == 0, where + ": sb_buffer_free did not reset the buffer");
    expect(Json::accept(text), where + ": the report is not JSON");
    if (status != SB_OK)
    {
        const auto report = Json::parse(text);
        expect(report.is_object() && report.value("status", "") == "error", where + ": a failure report lacks its error status");
    }
}

std::int64_t stride()
{
    const char *raw = std::getenv("SWITCHBOARD_ALLOC_FAULT_STRIDE");
    if (raw == nullptr || *raw == '\0')
    {
        return 1;
    }
    char *end = nullptr;
    errno = 0;
    const auto value = std::strtoll(raw, &end, 10);
    if (errno != 0 || end == raw || *end != '\0' || value < 1 || value > 1000000)
    {
        throw std::runtime_error("SWITCHBOARD_ALLOC_FAULT_STRIDE must be a positive integer");
    }
    return value;
}

// Calls attempt(after, sticky) for after = 0, step, 2 * step, ... until an attempt sees no injected failure.
template <typename Attempt> void sweep(const std::string &name, const bool sticky_mode, const std::int64_t step, Attempt attempt)
{
    for (std::int64_t after = 0; after < LIMIT; after += step)
    {
        if (!attempt(after, sticky_mode))
        {
            std::cout << name << (sticky_mode ? " (sticky)" : " (one-shot)") << ": completed after " << after << " allocations\n";
            return;
        }
    }
    throw std::runtime_error(name + ": the allocation count did not converge");
}

bool create_attempt(const std::int64_t after, const bool sticky_mode)
{
    Scratch directory;
    const auto configuration = inert(directory);
    sb_runtime *runtime = nullptr;
    sb_buffer error{nullptr, 0};
    const auto status = armed(after, sticky_mode,
                              [&]
                              {
                                  return sb_runtime_create(bytes(configuration), configuration.size(), &runtime, &error);
                              });
    const bool failed = injected();
    check_result(status, error, false, "sb_runtime_create");
    expect((status == SB_OK) == (runtime != nullptr), "sb_runtime_create published a runtime inconsistently with its status");
    sb_runtime_destroy(runtime);
    expect(failed || status == SB_OK, "sb_runtime_create failed without an injected fault");
    return failed;
}

bool destroy_attempt(const std::int64_t after, const bool sticky_mode)
{
    Scratch directory;
    const auto configuration = inert(directory);
    sb_runtime *runtime = nullptr;
    sb_buffer output{nullptr, 0};
    expect(sb_runtime_create(bytes(configuration), configuration.size(), &runtime, &output) == SB_OK, "cannot create a runtime to destroy");
    expect(sb_runtime_call(runtime, bytes(CALL), CALL.size(), SB_ASSISTANT, &output) == SB_OK, "cannot exercise a runtime to destroy");
    sb_buffer_free(&output);
    (void)armed(after, sticky_mode,
                [&]
                {
                    sb_runtime_destroy(runtime);
                    return SB_OK;
                });
    return injected();
}

class Engine
{

public:
    Engine()
    {
        const auto configuration = inert(directory_);
        sb_buffer error{nullptr, 0};
        expect(sb_runtime_create(bytes(configuration), configuration.size(), &runtime_, &error) == SB_OK, "cannot create the shared runtime");
    }

    ~Engine()
    {
        sb_runtime_destroy(runtime_);
    }

    Engine(const Engine &) = delete;
    Engine &operator=(const Engine &) = delete;

    bool catalog(const std::int64_t after, const bool sticky_mode)
    {
        sb_buffer output{nullptr, 0};
        const auto status = armed(after, sticky_mode,
                                  [&]
                                  {
                                      return sb_runtime_catalog(runtime_, &output);
                                  });
        return finish(status, output, "sb_runtime_catalog");
    }

    bool call(const std::int64_t after, const bool sticky_mode)
    {
        sb_buffer output{nullptr, 0};
        const auto status = armed(after, sticky_mode,
                                  [&]
                                  {
                                      return sb_runtime_call(runtime_, bytes(CALL), CALL.size(), SB_ASSISTANT, &output);
                                  });
        return finish(status, output, "sb_runtime_call");
    }

    bool call_outcome(const std::int64_t after, const bool sticky_mode)
    {
        sb_buffer output{nullptr, 0};
        sb_outcome outcome{};
        const auto status = armed(after, sticky_mode,
                                  [&]
                                  {
                                      return sb_runtime_call_outcome(runtime_, bytes(CALL), CALL.size(), SB_ASSISTANT, &outcome, &output);
                                  });
        expect(outcome.status >= SB_OUTCOME_OK && outcome.status <= SB_OUTCOME_ERROR && outcome.effect >= SB_EFFECT_NONE && outcome.effect <= SB_EFFECT_FAILED,
               "sb_runtime_call_outcome: typed outcome is out of range");
        expect(std::memchr(outcome.approval_id, 0, sizeof(outcome.approval_id)) != nullptr &&
                   std::memchr(outcome.action_id, 0, sizeof(outcome.action_id)) != nullptr,
               "sb_runtime_call_outcome: an identifier is not terminated");
        expect(status != SB_OK || outcome.effect == SB_EFFECT_NONE, "sb_runtime_call_outcome: a read call reported an effect");
        return finish(status, output, "sb_runtime_call_outcome");
    }

    bool rpc(const std::int64_t after, const bool sticky_mode)
    {
        sb_buffer output{nullptr, 0};
        const auto status = armed(after, sticky_mode,
                                  [&]
                                  {
                                      return sb_runtime_rpc(runtime_, bytes(RPC), RPC.size(), &output);
                                  });
        return finish(status, output, "sb_runtime_rpc");
    }

    // After every sweep the runtime must still answer and its audit chain must still verify.
    void check_usable()
    {
        sb_buffer output{nullptr, 0};
        const std::string verify = R"({"tool":"audit_verify","arguments":{}})";
        expect(sb_runtime_call(runtime_, bytes(CALL), CALL.size(), SB_ASSISTANT, &output) == SB_OK, "the runtime stopped answering after allocation faults");
        const std::string status(reinterpret_cast<const char *>(output.data), output.length);
        sb_buffer_free(&output);
        expect(Json::parse(status).value("status", "") == "ok", "a status call failed after allocation faults");
        expect(sb_runtime_call(runtime_, bytes(verify), verify.size(), SB_ASSISTANT, &output) == SB_OK,
               "the audit check could not run after allocation faults");
        const std::string audit(reinterpret_cast<const char *>(output.data), output.length);
        sb_buffer_free(&output);
        const auto report = Json::parse(audit);
        expect(report.value("status", "") == "ok" && report.contains("data") && report["data"].value("chain_matches", false),
               "the audit chain did not verify after allocation faults; look for a silently truncated digest or identifier");
    }

private:
    bool finish(const sb_status status, sb_buffer &output, const std::string &where)
    {
        const bool failed = injected();
        check_result(status, output, true, where);
        expect(failed || status == SB_OK, where + " failed without an injected fault");
        return failed;
    }

    Scratch directory_;
    sb_runtime *runtime_ = nullptr;
};

// The first allocation inside sb_runtime_create must reach the replacement, or the sweep would prove nothing.
bool replacement_reaches_library()
{
    return create_attempt(0, false);
}
}  // namespace

int main()
{
    try
    {
        // The first backtrace loads the unwinder; do it before any allocation can fail.
        (void)inside_unrecoverable_frame();
        if (!replacement_reaches_library())
        {
            std::cout << "SKIP: replacing global operator new does not reach the shared library on this platform\n";
            return 77;
        }
        const auto step = stride();
        for (const bool sticky_mode : {false, true})
        {
            const auto current = sticky_mode ? step * STICKY_FACTOR : step;
            sweep("sb_runtime_create", sticky_mode, current, create_attempt);
            sweep("sb_runtime_destroy", sticky_mode, current, destroy_attempt);
            Engine engine;
            sweep("sb_runtime_catalog", sticky_mode, current,
                  [&](const std::int64_t after, const bool mode)
                  {
                      return engine.catalog(after, mode);
                  });
            sweep("sb_runtime_call", sticky_mode, current,
                  [&](const std::int64_t after, const bool mode)
                  {
                      return engine.call(after, mode);
                  });
            sweep("sb_runtime_call_outcome", sticky_mode, current,
                  [&](const std::int64_t after, const bool mode)
                  {
                      return engine.call_outcome(after, mode);
                  });
            sweep("sb_runtime_rpc", sticky_mode, current,
                  [&](const std::int64_t after, const bool mode)
                  {
                      return engine.rpc(after, mode);
                  });
            engine.check_usable();
        }
        std::cout << "native allocation fault tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        remaining.store(-1, std::memory_order_relaxed);
        std::cerr << "native allocation fault test failure: " << error.what() << '\n';
        return 1;
    }
}

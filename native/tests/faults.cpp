// Fault injection for the native engine.
//
// A fault transport fails in every documented way (transport, byte limit, deadline and cancellation errors,
// server errors, malformed, oversized, deeply nested and non-UTF-8 bodies, and an unexpected exception), and
// storage is made unwritable, read-only, newer, corrupt or locked. Every fault must surface as a reported
// outcome or a switchboard::Error (an error report through the C interface), never a crash; writes are never
// repeated automatically; and the audit chain must still verify afterwards.
#include "support.hpp"
#include "switchboard/switchboard.h"

#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iterator>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

#include <sys/stat.h>
#include <unistd.h>

using namespace switchboard;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::expect_error_code;
using switchboard::test::FakeHttp;

namespace
{
// Bodies carry this marker so a test can prove that provider content never leaks into an outcome.
const std::string MARKER = "synthetic-private-detail";
const std::string WLED = "http://192.168.50.20";

enum class Fault : std::uint8_t
{
    transport,
    limit,
    deadline,
    cancelled,
    server_error,
    malformed_body,
    oversized_body,
    deep_body,
    invalid_utf8_body,
    unexpected
};

struct Case
{
    const char *name;
    Fault fault;
    // contracts/README.md: transport, limit, cancelled and deadline errors and unexpected exceptions leave a
    // write uncertain; any other native error asserts that the effect did not happen.
    bool uncertain;
    // Body-parsing failures raise the "invalid" code inside the handler; the outcome may report invalid or error.
    bool invalid_code;
};

const std::array<Case, 10> CASES = {{{"transport error", Fault::transport, true, false},
                                     {"byte limit", Fault::limit, true, false},
                                     {"deadline", Fault::deadline, true, false},
                                     {"cancellation", Fault::cancelled, true, false},
                                     {"server error status", Fault::server_error, false, false},
                                     {"malformed body", Fault::malformed_body, false, true},
                                     {"oversized body", Fault::oversized_body, true, false},
                                     {"deeply nested body", Fault::deep_body, false, true},
                                     {"non-UTF-8 body", Fault::invalid_utf8_body, false, true},
                                     {"unexpected exception", Fault::unexpected, true, false}}};

class FaultTransport final : public HttpTransport
{

public:
    FaultTransport(const Fault fault, std::shared_ptr<int> requests) : fault_(fault), requests_(std::move(requests))
    {
    }

    HttpResponse request(const HttpRequest &) override
    {
        ++*requests_;
        switch (fault_)
        {
        case Fault::transport:
            throw Error("transport", "synthetic connection reset after the request was sent");
        case Fault::limit:
            throw Error("limit", "synthetic response exceeded its byte cap");
        case Fault::deadline:
            throw Error("deadline", "synthetic deadline passed during the transfer");
        case Fault::cancelled:
            throw Error("cancelled", "synthetic cancellation during the transfer");
        case Fault::server_error:
            return {500, {}, "{\"error\":\"" + MARKER + "\"}"};
        case Fault::malformed_body:
            return {200, {}, "{\"state\":\"" + MARKER};
        case Fault::oversized_body:
            return {200, {}, "{\"padding\":\"" + MARKER + std::string(MAX_REQUEST_BYTES, 'x') + "\"}"};
        case Fault::deep_body:
            return {200, {}, std::string(100, '[') + "\"" + MARKER + "\"" + std::string(100, ']')};
        case Fault::invalid_utf8_body:
            return {200, {}, "{\"text\":\"" + MARKER + "\xff\xfe\"}"};
        case Fault::unexpected:
            throw std::runtime_error("synthetic unexpected transport failure");
        }
        throw std::logic_error("unhandled synthetic fault");
    }

private:
    Fault fault_;
    std::shared_ptr<int> requests_;
};

// fault_read and fault_write each make one request through the runtime's transport and parse the body.
Extension fault_tools()
{
    return [](Context &context, Registry &registry)
    {
        const auto fetch = [&context](const std::string &method)
        {
            return [&context, method](const Json &) -> Json
            {
                HttpRequest request;
                request.method = method;
                request.url = "https://device.invalid/state";
                request.max_bytes = 4096;
                if (method == "POST")
                {
                    request.body = "{\"on\":false}";
                }
                return Json{{"body", response_json(context.http.request(request))}};
            };
        };
        registry.add({"fault_read", "Synthetic read through the fault transport.", Tier::read, object_schema(Json::object()), fetch("GET")});
        registry.add({"fault_write", "Synthetic draft-tier write through the fault transport.", Tier::draft, object_schema(Json::object()), fetch("POST")});
    };
}

bool reported_failure(const Json &report, const Case &item)
{
    const auto status = report.value("status", "");
    return (status == "error" || (item.invalid_code && status == "invalid")) && report.dump().find(MARKER) == std::string::npos;
}

bool chain_matches(Runtime &runtime)
{
    const auto report = runtime.call("audit_verify", Json::object());
    return report.value("status", "") == "ok" && report["data"].value("chain_matches", false);
}

template <typename Function> void expect_native_error(Function function, const std::string &message)
{
    try
    {
        function();
    }
    catch (const Error &)
    {
        return;
    }
    throw std::runtime_error(message + " (no native error was raised)");
}

bool running_as_root()
{
    if (geteuid() == 0)
    {
        std::cout << "SKIP: permission faults are not observable as root\n";
        return true;
    }
    return false;
}

const std::uint8_t *bytes(const std::string &text)
{
    return reinterpret_cast<const std::uint8_t *>(text.data());
}

// Creating a runtime through the C interface must fail with an error report and no published handle.
void expect_c_creation_failure(const Json &configuration, const std::string &code, const std::string &message)
{
    const auto text = configuration.dump();
    sb_runtime *runtime = nullptr;
    sb_buffer report{nullptr, 0};
    const auto status = sb_runtime_create(bytes(text), text.size(), &runtime, &report);
    const std::string body = report.data == nullptr ? std::string() : std::string(reinterpret_cast<const char *>(report.data), report.length);
    sb_buffer_free(&report);
    if (runtime != nullptr)
    {
        sb_runtime_destroy(runtime);
    }
    expect(status == SB_RUNTIME_ERROR && runtime == nullptr, message + ": wrong C status or a published runtime");
    expect(Json::accept(body), message + ": the C error report is not JSON");
    const auto parsed = Json::parse(body);
    expect(parsed.value("status", "") == "error" && parsed.value("code", "") == code, message + ": the C error report has the wrong code");
}

std::string contents(const std::filesystem::path &path)
{
    std::ifstream input(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

void transport_faults_are_reported_and_never_repeated()
{
    for (const auto &item : CASES)
    {
        const std::string name = item.name;
        Directory directory;
        auto requests = std::make_shared<int>(0);
        Runtime runtime(test::configuration(directory, {}), std::make_unique<FaultTransport>(item.fault, requests), {}, fault_tools());

        const auto read = runtime.call("fault_read", Json::object());
        expect(reported_failure(read, item), name + ": a read fault was not reported as a sanitized failure");
        expect(!read.contains("action") && *requests == 1, name + ": a read was journaled, retried or skipped");

        const auto write = runtime.call("fault_write", Json::object());
        expect(reported_failure(write, item), name + ": a write fault was not reported as a sanitized failure");
        expect(write.contains("action") && write["action"].value("state", "") == (item.uncertain ? "uncertain" : "failed"),
               name + ": the write was not journaled with the contract classification");
        expect(*requests == 2, name + ": a failed write was retried automatically");

        const auto repeat = runtime.call("fault_write", Json::object());
        if (item.uncertain)
        {
            expect(repeat.value("status", "") == "denied" && *requests == 2, name + ": an uncertain write was repeated before reconciliation");
            const auto reconciled = runtime.reconcile(write["action"]["id"].get<std::string>(), "not_applied", "synthetic fault reviewed by the test");
            expect(reconciled.value("status", "") == "ok", name + ": the uncertain write could not be reconciled");
            const auto after = runtime.call("fault_write", Json::object());
            expect(reported_failure(after, item) && *requests == 3, name + ": reconciliation did not lift the duplicate guard");
        }
        else
        {
            expect(reported_failure(repeat, item) && *requests == 3, name + ": a write that asserted no effect could not be repeated");
        }
        expect(chain_matches(runtime), name + ": the audit chain did not verify after transport faults");
    }
}

void adapter_reads_report_transport_faults()
{
    const Json wled = {{"timeout_seconds", 5},
                       {"max_response_bytes", 65536},
                       {"devices", Json::array({{{"name", "desk"},
                                                 {"base_url", WLED},
                                                 {"allow_http", true},
                                                 {"allow_control", false},
                                                 {"allow_global", false},
                                                 {"max_brightness", 128},
                                                 {"max_transition_deciseconds", 100},
                                                 {"segments", Json::array({0})},
                                                 {"presets", Json::array()}}})}};
    for (const auto &item : CASES)
    {
        Directory directory;
        auto requests = std::make_shared<int>(0);
        Runtime runtime(test::configuration(directory, {"wled"}, {{"wled", wled}}), std::make_unique<FaultTransport>(item.fault, requests));
        const auto report = runtime.call("wled_status", {{"device", "desk"}});
        expect(reported_failure(report, item) || (report.value("status", "") == "invalid" && report.dump().find(MARKER) == std::string::npos),
               std::string(item.name) + ": the WLED adapter did not report a sanitized failure");
        expect(*requests == 1, std::string(item.name) + ": the WLED adapter retried or skipped its read");
        expect(chain_matches(runtime), std::string(item.name) + ": the audit chain did not verify after an adapter fault");
    }
}

void unwritable_data_directory_is_refused()
{
    if (running_as_root())
    {
        return;
    }
    Directory directory;
    const auto locked = directory.path / "locked";
    expect(std::filesystem::create_directory(locked) && chmod(locked.c_str(), 0500) == 0, "cannot arrange an unwritable parent directory");
    auto configuration = test::configuration(directory, {});
    configuration["switchboard"]["data_dir"] = (locked / "state").string();
    expect_native_error(
        [&]
        {
            Runtime runtime(configuration, std::make_unique<FakeHttp>());
        },
        "a data directory under an unwritable parent was accepted");
    expect(!std::filesystem::exists(locked / "state"), "a refused data directory was partially created");
    expect_c_creation_failure(configuration, "filesystem", "unwritable data directory");
    expect(chmod(locked.c_str(), 0700) == 0, "cannot restore the test directory");
}

void read_only_reopen_is_refused_then_recovers()
{
    if (running_as_root())
    {
        return;
    }
    Directory directory;
    const auto configuration = test::configuration(directory, {});
    {
        Runtime runtime(configuration, std::make_unique<FakeHttp>());
        expect(runtime.call("switchboard_status", Json::object()).value("status", "") == "ok", "the initial runtime did not work");
    }
    const auto state = directory.path / "native-state.sqlite3";
    expect(chmod(state.c_str(), 0400) == 0, "cannot make the state file read-only");
    expect_native_error(
        [&]
        {
            Runtime reopened(configuration, std::make_unique<FakeHttp>());
        },
        "a read-only state file was reopened for writing");
    expect(chmod(state.c_str(), 0600) == 0, "cannot restore the state file");
    Runtime restored(configuration, std::make_unique<FakeHttp>());
    expect(chain_matches(restored), "the audit chain did not verify after a refused read-only reopen");
}

void newer_schema_is_refused()
{
    Directory directory;
    const auto configuration = test::configuration(directory, {});
    {
        Runtime runtime(configuration, std::make_unique<FakeHttp>());
    }
    {
        Database editor(directory.path / "native-state.sqlite3");
        (void)editor.execute("PRAGMA user_version = " + std::to_string(STATE_SCHEMA_VERSION + 1));
    }
    expect_error_code(
        [&]
        {
            Runtime newer(configuration, std::make_unique<FakeHttp>());
        },
        "database", "state written by a newer schema was opened");
}

void corrupt_state_is_refused_and_left_untouched()
{
    Directory directory;
    const auto state = directory.path / "native-state.sqlite3";
    std::string garbage;
    while (garbage.size() < 4096)
    {
        garbage += "synthetic corrupt state: this is not a SQLite database\n";
    }
    {
        std::ofstream output(state, std::ios::binary);
        output << garbage;
    }
    expect(chmod(state.c_str(), 0600) == 0, "cannot protect the corrupt state file");
    const auto configuration = test::configuration(directory, {});
    expect_error_code(
        [&]
        {
            Runtime corrupt(configuration, std::make_unique<FakeHttp>());
        },
        "database", "a corrupt state file was opened");
    expect_c_creation_failure(configuration, "database", "corrupt state file");
    expect(contents(state) == garbage, "a refused corrupt state file was modified");
}

// Another connection holds the write lock; the runtime's write waits for its busy timeout and then fails.
void locked_database_is_reported_then_recovers()
{
    Directory directory;
    const auto configuration = test::configuration(directory, {});
    Runtime runtime(configuration, std::make_unique<FakeHttp>());
    expect(runtime.call("switchboard_status", Json::object()).value("status", "") == "ok", "the initial call failed");
    {
        Database blocker(directory.path / "native-state.sqlite3");
        blocker.begin();
        bool reported = false;
        try
        {
            reported = runtime.call("switchboard_status", Json::object()).value("status", "") == "error";
        }
        catch (const Error &error)
        {
            reported = error.code() == "database";
        }
        blocker.rollback();
        expect(reported, "a locked database was not reported as an error");
    }
    expect(runtime.call("switchboard_status", Json::object()).value("status", "") == "ok", "the runtime did not recover after the lock was released");
    expect(chain_matches(runtime), "the audit chain did not verify after a lock timeout");
}

void locked_database_is_reported_through_the_c_interface()
{
    Directory directory;
    const auto text = test::configuration(directory, {}).dump();
    sb_runtime *runtime = nullptr;
    sb_buffer output{nullptr, 0};
    expect(sb_runtime_create(bytes(text), text.size(), &runtime, &output) == SB_OK && runtime != nullptr, "the C runtime could not be created");
    const std::string request = "{\"tool\":\"switchboard_status\",\"arguments\":{}}";
    sb_status status = SB_OK;
    std::string body;
    {
        Database blocker(directory.path / "native-state.sqlite3");
        blocker.begin();
        status = sb_runtime_call(runtime, bytes(request), request.size(), SB_ASSISTANT, &output);
        body = output.data == nullptr ? std::string() : std::string(reinterpret_cast<const char *>(output.data), output.length);
        sb_buffer_free(&output);
        blocker.rollback();
    }
    expect(Json::accept(body), "a C result under a locked database is not JSON");
    const auto report = Json::parse(body);
    expect((status == SB_RUNTIME_ERROR && report.value("code", "") == "database") || (status == SB_OK && report.value("status", "") == "error"),
           "a locked database was not reported through the C interface");
    expect(sb_runtime_call(runtime, bytes(request), request.size(), SB_ASSISTANT, &output) == SB_OK, "the C runtime did not recover");
    sb_buffer_free(&output);
    sb_runtime_destroy(runtime);
}
}  // namespace

int main()
{
    try
    {
        transport_faults_are_reported_and_never_repeated();
        adapter_reads_report_transport_faults();
        unwritable_data_directory_is_refused();
        read_only_reopen_is_refused_then_recovers();
        newer_schema_is_refused();
        corrupt_state_is_refused_and_left_untouched();
        locked_database_is_reported_then_recovers();
        locked_database_is_reported_through_the_c_interface();
        std::cout << "native fault tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "native fault test failure: " << error.what() << '\n';
        return 1;
    }
}

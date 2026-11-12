#include "switchboard/native.hpp"

#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <optional>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

#include <signal.h>

namespace
{
using switchboard::Error;
using switchboard::Json;
using switchboard::Runtime;

const char *const USAGE = "Usage:\n"
                          "  switchboard-native --version\n"
                          "  switchboard-native serve-mcp --broker SOCKET\n"
                          "  switchboard-native backup verify DIR\n"
                          "  switchboard-native --config CONFIG.json COMMAND [ARGUMENTS]\n"
                          "Commands: catalog | call TOOL JSON | approvals show ID | approve ID | deny ID | run [--once] | serve-mcp | broker\n"
                          "          actions [--state STATE] [--limit N] | actions show ID | reconcile ID applied|not-applied NOTE\n"
                          "          kill-switch on|off|status | backup create DIR | backup verify DIR | purge [--dry-run]\n";

volatile std::sig_atomic_t stop_requested = 0;
std::atomic<Runtime *> stopping_runtime{nullptr};
static_assert(std::atomic<Runtime *>::is_always_lock_free, "the termination handler must use lock-free state");
constexpr std::array<int, 2> STOP_SIGNALS = {SIGINT, SIGTERM};

std::string configuration_text(const std::string &path)
{
    std::ifstream input(path, std::ios::binary);
    if (!input)
    {
        throw Error("configuration", "cannot open the native configuration file");
    }
    std::string result;
    char buffer[8192];
    while (input)
    {
        input.read(buffer, sizeof(buffer));
        result.append(buffer, static_cast<std::size_t>(input.gcount()));
        if (result.size() > switchboard::MAX_REQUEST_BYTES)
        {
            throw Error("limit", "native configuration exceeds its byte limit");
        }
    }
    if (!input.eof())
    {
        throw Error("configuration", "native configuration could not be read completely");
    }
    return result;
}

bool protocol_line(std::string &line)
{
    line.clear();
    char character = 0;
    while (std::cin.get(character))
    {
        if (character == '\n')
        {
            return true;
        }
        if (line.size() == switchboard::MAX_REQUEST_BYTES)
        {
            throw Error("limit", "native protocol input exceeds its byte limit; server is stopping");
        }
        line.push_back(character);
    }
    if (std::cin.bad())
    {
        throw Error("protocol", "native protocol input failed");
    }
    return !line.empty();
}

void print(const Json &value)
{
    std::cout << value.dump() << '\n';
    std::cout.flush();
    if (!std::cout)
    {
        throw Error("output", "native output could not be written");
    }
}

int serve(switchboard::Runtime &runtime)
{
    std::string line;
    while (protocol_line(line))
    {
        Json response;
        try
        {
            response = runtime.rpc(switchboard::parse_json(line));
        }
        catch (const Error &)
        {
            response = {{"jsonrpc", "2.0"}, {"id", nullptr}, {"error", {{"code", -32700}, {"message", "native request could not be parsed or completed"}}}};
        }
        catch (const std::exception &)
        {
            std::fputs("ERROR: unexpected native protocol failure\n", stderr);
            response = {{"jsonrpc", "2.0"}, {"id", nullptr}, {"error", {{"code", -32603}, {"message", "native internal error"}}}};
        }
        if (!response.is_null())
        {
            print(response);
        }
    }
    return 0;
}

bool incomplete(const Json &report)
{
    if (report.value("cancelled", false))
    {
        return true;
    }
    for (const auto &job : report.at("jobs"))
    {
        if (!job.at("error").is_null())
        {
            return true;
        }
    }
    return false;
}

// Async-signal-safe: a sig_atomic_t store and lock-free atomic operations only. Cancellation aborts web transfers
// of the job in progress and defers the rest of the tick.
void request_stop(const int) noexcept
{
    stop_requested = 1;
    Runtime *runtime = stopping_runtime.load();
    if (runtime != nullptr)
    {
        runtime->cancel();
    }
}

class StopHandlers
{

public:
    explicit StopHandlers(Runtime &runtime)
    {
        stopping_runtime.store(&runtime);
        for (std::size_t index = 0; index < STOP_SIGNALS.size(); ++index)
        {
            if (sigaction(STOP_SIGNALS[index], nullptr, &previous_[index]) != 0)
            {
                restore();
                throw Error("signal", "the native runner could not inspect its termination handlers");
            }
            saved_[index] = true;
            // A signal ignored on entry, as for some background jobs, stays ignored.
            if ((previous_[index].sa_flags & SA_SIGINFO) == 0 && previous_[index].sa_handler == SIG_IGN)
            {
                continue;
            }
            struct sigaction action
            {
            };
            action.sa_handler = request_stop;
            sigemptyset(&action.sa_mask);
            // The handler resets itself: a second signal takes the default action and ends the process at once.
            action.sa_flags = SA_RESETHAND;
            if (sigaction(STOP_SIGNALS[index], &action, nullptr) != 0)
            {
                restore();
                throw Error("signal", "the native runner could not install its termination handlers");
            }
        }
    }

    ~StopHandlers()
    {
        restore();
    }

    StopHandlers(const StopHandlers &) = delete;
    StopHandlers &operator=(const StopHandlers &) = delete;

private:
    std::array<struct sigaction, STOP_SIGNALS.size()> previous_{};
    std::array<bool, STOP_SIGNALS.size()> saved_{};

    void restore() noexcept
    {
        for (std::size_t index = 0; index < STOP_SIGNALS.size(); ++index)
        {
            if (saved_[index] && sigaction(STOP_SIGNALS[index], &previous_[index], nullptr) != 0)
            {
                std::fputs("ERROR: the native runner could not restore its previous signal handling\n", stderr);
            }
        }
        stopping_runtime.store(nullptr);
    }
};

int run(Runtime &runtime, const bool once)
{
    const StopHandlers handlers(runtime);
    if (once)
    {
        const auto report = runtime.tick();
        print(report);
        return incomplete(report) ? 1 : 0;
    }
    // The stop flag is checked between ticks; sleeping in short steps keeps shutdown prompt.
    while (stop_requested == 0)
    {
        print(runtime.tick());
        for (int second = 0; second < 30 && stop_requested == 0; ++second)
        {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }
    }
    std::cerr << "switchboard-native: stopped after a termination request\n";
    return 0;
}

int report_status(const Json &report)
{
    print(report);
    const auto status = report.value("status", "");
    return status == "error" || status == "invalid" || status == "denied" ? 1 : 0;
}

std::filesystem::path absolute_path(const std::string &value)
{
    if (value.empty() || value.find('\0') != std::string::npos)
    {
        throw Error("arguments", "native paths must be nonempty");
    }
    if (value == "~" || value.rfind("~/", 0) == 0)
    {
        return switchboard::expand_path(value);
    }
    std::error_code code;
    const auto result = std::filesystem::absolute(value, code);
    if (code)
    {
        throw Error("arguments", "native path could not be resolved");
    }
    return result.lexically_normal();
}

Json action_filter(const std::vector<std::string> &options)
{
    Json filter = Json::object();
    for (std::size_t index = 0; index < options.size(); index += 2)
    {
        if (index + 1 >= options.size())
        {
            throw Error("arguments", "native action options need a value");
        }
        const auto &option = options[index];
        const auto &value = options[index + 1];
        if (option == "--state" && !filter.contains("state"))
        {
            filter["state"] = value;
        }
        else if (option == "--limit" && !filter.contains("limit"))
        {
            std::int64_t limit = 0;
            const auto parsed = std::from_chars(value.data(), value.data() + value.size(), limit);
            if (parsed.ec != std::errc() || parsed.ptr != value.data() + value.size())
            {
                throw Error("arguments", "native --limit must be a whole number");
            }
            filter["limit"] = limit;
        }
        else
        {
            throw Error("arguments", "native actions accepts --state STATE and --limit N, once each");
        }
    }
    return filter;
}

// The runtime has already validated the section; the broker command additionally requires all four settings.
switchboard::BrokerOptions broker_options(const Json &configuration)
{
    const auto section = configuration.contains("broker") ? configuration.at("broker") : Json::object();
    for (const auto *key : {"client_socket", "operator_socket", "client_uids", "operator_uids"})
    {
        if (!section.is_object() || !section.contains(key))
        {
            throw Error("configuration", "the native broker needs broker.client_socket, broker.operator_socket, broker.client_uids and broker.operator_uids");
        }
    }
    switchboard::BrokerOptions options;
    options.client_socket = switchboard::expand_path(section.at("client_socket").get<std::string>());
    options.operator_socket = switchboard::expand_path(section.at("operator_socket").get<std::string>());
    for (const auto &uid : section.at("client_uids"))
    {
        options.client_uids.push_back(static_cast<std::uint32_t>(uid.get<std::uint64_t>()));
    }
    for (const auto &uid : section.at("operator_uids"))
    {
        options.operator_uids.push_back(static_cast<std::uint32_t>(uid.get<std::uint64_t>()));
    }
    return options;
}

int execute(Runtime &runtime, const Json &configuration, const std::vector<std::string> &command)
{
    const auto &name = command.front();
    const auto count = command.size();
    if (name == "catalog" && count == 1)
    {
        return report_status(runtime.catalog());
    }
    if (name == "call" && count == 3)
    {
        return report_status(runtime.call(command[1], switchboard::parse_json(command[2]), switchboard::Origin::human));
    }
    if (name == "approve" && count == 2)
    {
        return report_status(runtime.approve(command[1]));
    }
    if (name == "deny" && count == 2)
    {
        // "denied" is this command's success report; refusals come back as "invalid" or "error".
        const auto report = runtime.deny(command[1]);
        print(report);
        return report.value("status", "") == "denied" ? 0 : 1;
    }
    // The record's own "status" is the approval state, not an operation result; a missing approval raises.
    if (name == "approvals" && count == 3 && command[1] == "show")
    {
        print(runtime.approval(command[2]));
        return 0;
    }
    if (name == "serve-mcp" && count == 1)
    {
        return serve(runtime);
    }
    if (name == "run" && (count == 1 || (count == 2 && command[1] == "--once")))
    {
        return run(runtime, count == 2);
    }
    if (name == "actions" && count == 3 && command[1] == "show")
    {
        return report_status(runtime.actions(Json{{"id", command[2]}}));
    }
    if (name == "actions")
    {
        return report_status(runtime.actions(action_filter(std::vector<std::string>(command.begin() + 1, command.end()))));
    }
    if (name == "reconcile" && count == 4 && (command[2] == "applied" || command[2] == "not-applied"))
    {
        return report_status(runtime.reconcile(command[1], command[2] == "applied" ? "applied" : "not_applied", command[3]));
    }
    if (name == "kill-switch" && count == 2 && (command[1] == "on" || command[1] == "off" || command[1] == "status"))
    {
        std::optional<bool> enable;
        if (command[1] != "status")
        {
            enable = command[1] == "on";
        }
        return report_status(runtime.kill_switch(enable));
    }
    if (name == "backup" && count == 3 && command[1] == "create")
    {
        return report_status(runtime.backup(absolute_path(command[2])));
    }
    if (name == "purge" && (count == 1 || (count == 2 && command[1] == "--dry-run")))
    {
        return report_status(runtime.purge(count == 2));
    }
    if (name == "broker" && count == 1)
    {
        return switchboard::run_broker(runtime, broker_options(configuration));
    }
    throw Error("arguments", "native command or argument count is invalid");
}

int dispatch(std::vector<std::string> command)
{
    if (command.size() == 1 && command.front() == "--version")
    {
        std::cout << "switchboard-native 0.1.0\n";
        return 0;
    }
    std::optional<std::string> configuration_path;
    if (command.size() >= 2 && command.front() == "--config")
    {
        configuration_path = command[1];
        command.erase(command.begin(), command.begin() + 2);
    }
    // These two never load configuration: the shim holds no secrets and verification needs no runtime.
    if (command.size() == 3 && command[0] == "serve-mcp" && command[1] == "--broker")
    {
        return switchboard::run_broker_shim(absolute_path(command[2]));
    }
    if (command.size() == 3 && command[0] == "backup" && command[1] == "verify")
    {
        return report_status(Runtime::verify_backup(absolute_path(command[2])));
    }
    if (!configuration_path || command.empty())
    {
        std::cerr << USAGE;
        return 2;
    }
    const auto configuration = switchboard::parse_json(configuration_text(*configuration_path));
    Runtime runtime(configuration);
    return execute(runtime, configuration, command);
}
}  // namespace

int main(const int argc, char **argv)
{
    try
    {
        return dispatch(argc > 1 ? std::vector<std::string>(argv + 1, argv + argc) : std::vector<std::string>());
    }
    catch (const Error &error)
    {
        std::cerr << "ERROR: " << Json(error.what()).dump(-1, ' ', true) << '\n';
    }
    catch (const std::exception &)
    {
        std::cerr << "ERROR: unexpected native client failure; inspect state before retrying external effects\n";
    }
    return 1;
}

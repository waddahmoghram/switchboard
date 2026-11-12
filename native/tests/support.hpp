#pragma once

#include "switchboard/native.hpp"

#include <chrono>
#include <filesystem>
#include <functional>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <sys/stat.h>
#include <unistd.h>

namespace switchboard::test
{
inline void expect(const bool condition, const std::string &message)
{
    if (!condition)
    {
        throw std::runtime_error(message);
    }
}

class Directory
{

public:
    Directory()
    {
        const auto token = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::temp_directory_path() / ("switchboard-native-test-" + std::to_string(getpid()) + "-" + std::to_string(token));
        if (!std::filesystem::create_directory(path))
        {
            throw std::runtime_error("cannot create private test directory");
        }
        if (chmod(path.c_str(), 0700) != 0)
        {
            throw std::runtime_error("cannot protect test directory");
        }
    }

    ~Directory()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: native test directory cleanup failed\n";
        }
    }

    Directory(const Directory &) = delete;
    Directory &operator=(const Directory &) = delete;
    std::filesystem::path path;
};

struct Route
{
    std::string method;
    std::string url;
    HttpResponse response;
};

class FakeHttp final : public HttpTransport
{

public:
    std::vector<Route> routes;
    std::vector<HttpRequest> requests;

    HttpResponse request(const HttpRequest &input) override
    {
        requests.push_back(input);
        if (routes.empty() || routes.front().method != input.method || routes.front().url != input.url)
        {
            throw Error("test_route", "unexpected fake HTTP request");
        }
        auto result = routes.front().response;
        routes.erase(routes.begin());
        return result;
    }

    void add(std::string method, std::string url, const Json &body, const long status = 200)
    {
        routes.push_back({std::move(method), std::move(url), {status, {}, body.dump()}});
    }
};

inline Json configuration(const Directory &directory, const std::vector<std::string> &connectors, const Json &settings = Json::object())
{
    return {{"switchboard", {{"connectors", connectors}, {"data_dir", directory.path.string()}}},
            {"connectors", settings},
            {"policy", {{"tools", Json::object()}}},
            {"mcp", {{"expose", {"*"}}}}};
}

template <typename Function> void expect_error_code(Function function, const std::string &code, const std::string &message)
{
    try
    {
        function();
    }
    catch (const Error &error)
    {
        expect(error.code() == code, message + " (got code " + error.code() + ")");
        return;
    }
    throw std::runtime_error(message + " (no native error was raised)");
}

// A settable runtime clock shared by a test and the runtime it configures.
class ManualClock
{

public:
    explicit ManualClock(const std::int64_t start = 1800000000) : value_(std::make_shared<std::int64_t>(start))
    {
    }

    [[nodiscard]] Clock clock() const
    {
        auto value = value_;
        return [value]
        {
            return *value;
        };
    }

    void advance(const std::int64_t seconds)
    {
        *value_ += seconds;
    }

    [[nodiscard]] std::int64_t now() const
    {
        return *value_;
    }

private:
    std::shared_ptr<std::int64_t> value_;
};

// Observations from synthetic tools. "during" runs inside the handler, after limits are installed.
struct SyntheticProbe
{
    int calls = 0;
    std::optional<ExecutionLimits> limits;
    std::function<void()> during;
};

inline Json synthetic_schema()
{
    return object_schema({{"mode", {{"type", "string"}, {"enum", Json::array({"ok", "fail", "uncertain", "effect_unknown", "crash", "web"})}}},
                          {"tag", {{"type", "string"}, {"maxLength", 32}}}},
                         {"mode"});
}

// In-process tools registered through the Runtime extension hook; they pass the same gate as built-in tools.
// synthetic_write is draft tier (allowed by default), synthetic_act is act tier (asks by default) and
// synthetic_read is read tier. Mode "web" makes one request through the runtime's (fake) transport.
inline Extension synthetic_tools(SyntheticProbe &probe)
{
    return [&probe](Context &context, Registry &registry)
    {
        const auto handler = [&probe, &context](const Json &arguments) -> Json
        {
            ++probe.calls;
            probe.limits = current_execution_limits();
            if (probe.during)
            {
                probe.during();
            }
            const auto mode = arguments.at("mode").get<std::string>();
            if (mode == "fail")
            {
                throw Error("synthetic_rejected", "the synthetic provider refused the write before applying it");
            }
            if (mode == "uncertain")
            {
                throw Error("uncertain", "the synthetic write may have been delivered");
            }
            if (mode == "effect_unknown")
            {
                return {{"written", nullptr}, {"effect_unknown", true}, {"confirmed", false}};
            }
            if (mode == "crash")
            {
                throw std::runtime_error("synthetic unexpected failure");
            }
            if (mode == "web")
            {
                HttpRequest request;
                request.method = "POST";
                request.url = "https://device.invalid/state";
                request.body = "{}";
                (void)context.http.request(request);
            }
            return {{"written", true}, {"confirmed", true}};
        };
        registry.add({"synthetic_write", "Synthetic draft-tier write for journal tests.", Tier::draft, synthetic_schema(), handler});
        registry.add({"synthetic_act", "Synthetic act-tier write for approval tests.", Tier::act, synthetic_schema(), handler});
        registry.add({"synthetic_read", "Synthetic read for journal tests.", Tier::read, object_schema(Json::object()),
                      [](const Json &)
                      {
                          return Json{{"read", true}};
                      }});
    };
}
}  // namespace switchboard::test

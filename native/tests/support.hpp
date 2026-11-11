#pragma once

#include "switchboard/native.hpp"

#include <chrono>
#include <filesystem>
#include <iostream>
#include <memory>
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
}  // namespace switchboard::test

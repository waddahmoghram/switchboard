#include "switchboard/native.hpp"

#include <chrono>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

namespace
{
using switchboard::Error;
using switchboard::Json;

std::string configuration_text(const char *path)
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

bool failed_jobs(const Json &report)
{
    for (const auto &job : report.at("jobs"))
    {
        if (!job.at("error").is_null())
        {
            return true;
        }
    }
    return false;
}
}  // namespace

int main(const int argc, char **argv)
{
    using switchboard::Runtime;
    if (argc == 2 && std::string(argv[1]) == "--version")
    {
        std::cout << "switchboard-native 0.1.0\n";
        return 0;
    }
    if (argc < 4 || std::string(argv[1]) != "--config")
    {
        std::cerr << "Usage: switchboard-native --config CONFIG.json catalog|call|approve|deny|run|serve-mcp [ARGUMENTS]\n";
        return 2;
    }
    try
    {
        Runtime runtime(switchboard::parse_json(configuration_text(argv[2])));
        const std::string command = argv[3];
        Json report;
        if (command == "catalog" && argc == 4)
        {
            report = runtime.catalog();
        }
        else if (command == "call" && argc == 6)
        {
            report = runtime.call(argv[4], switchboard::parse_json(argv[5]), switchboard::Origin::human);
        }
        else if (command == "approve" && argc == 5)
        {
            report = runtime.approve(argv[4]);
        }
        else if (command == "deny" && argc == 5)
        {
            report = runtime.deny(argv[4]);
        }
        else if (command == "serve-mcp" && argc == 4)
        {
            return serve(runtime);
        }
        else if (command == "run" && argc == 5 && std::string(argv[4]) == "--once")
        {
            report = runtime.tick();
            print(report);
            return failed_jobs(report) ? 1 : 0;
        }
        else if (command == "run" && argc == 4)
        {
            for (;;)
            {
                print(runtime.tick());
                std::this_thread::sleep_for(std::chrono::seconds(30));
            }
        }
        else
        {
            throw Error("arguments", "native command or argument count is invalid");
        }
        print(report);
        const auto status = report.value("status", "");
        return status == "error" || status == "invalid" || status == "denied" ? 1 : 0;
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

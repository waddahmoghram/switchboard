#include "support.hpp"

#include <cmath>
#include <fstream>
#include <memory>
#include <string>

#include <sys/stat.h>

using namespace switchboard;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::FakeHttp;

namespace
{
template <typename Function> void rejects(Function function)
{
    bool rejected = false;
    try
    {
        function();
    }
    catch (const Error &)
    {
        rejected = true;
    }
    expect(rejected, "native invalid input was accepted");
}

void schema_and_json()
{
    validate(Json("\xC3\xA9"), {{"type", "string"}, {"maxLength", 1}});
    rejects(
        []
        {
            validate(Json("\xC3\xA9x"), {{"type", "string"}, {"maxLength", 1}});
        });
    rejects(
        []
        {
            validate(true, {{"enum", Json::array({1})}});
        });
    rejects(
        []
        {
            check_schema({{"type", "string"}, {"maxLenght", 1}});
        });
    rejects(
        []
        {
            check_schema({{"enum", Json::array()}});
        });
    rejects(
        []
        {
            (void)parse_json("{\"key\":1,\"key\":2}");
        });
    rejects(
        []
        {
            (void)parse_json("{\"number\":1e9999}");
        });
    rejects(
        []
        {
            validate(Json(std::nan("")), Json::object());
        });
}

void dates_and_private_files()
{
    expect(timestamp(parse_timestamp("2028-02-29T10:00:00-08:00")) == "2028-02-29T18:00:00Z", "native date offset changed");
    rejects(
        []
        {
            (void)parse_timestamp("2026-02-29T10:00:00Z");
        });
    rejects(
        []
        {
            (void)parse_timestamp("2026-11-11T10:00:00");
        });
    Directory directory;
    const auto path = directory.path / "note.txt";
    {
        std::ofstream file(path);
        file << "Example note";
    }
    expect(chmod(path.c_str(), 0600) == 0, "cannot protect native test input");
    expect(read_private_file(directory.path, "note.txt", 100) == "Example note", "native private input changed");
    rejects(
        [&]
        {
            (void)read_private_file(directory.path, "../note.txt", 100);
        });
    rejects(
        [&]
        {
            (void)read_private_file(directory.path, "note.txt", 2);
        });
    std::filesystem::create_symlink(path, directory.path / "linked.txt");
    rejects(
        [&]
        {
            (void)read_private_file(directory.path, "linked.txt", 100);
        });
}

void approval_revalidation_and_budget()
{
    Directory directory;
    auto configuration = test::configuration(directory, {});
    configuration["policy"]["tools"]["switchboard_status"] = {{"decision", "ask"}};
    Runtime runtime(configuration, std::make_unique<FakeHttp>());
    const auto pending = runtime.call("switchboard_status", Json::object());
    expect(pending["status"] == "pending", "native ask policy did not queue");
    const auto id = pending["approval_id"].get<std::string>();
    expect(runtime.approve(id)["status"] == "ok", "native approval did not execute");
    expect(runtime.approve(id)["status"] == "error", "native approval replay succeeded");
    const auto second = runtime.call("switchboard_status", Json::object());
    configuration["policy"]["tools"]["switchboard_status"] = {{"decision", "deny"}};
    Runtime changed(configuration, std::make_unique<FakeHttp>());
    expect(changed.approve(second["approval_id"].get<std::string>())["status"] == "denied", "native approval bypassed current deny");

    Directory budget_directory;
    auto budget = test::configuration(budget_directory, {});
    budget["policy"]["tools"]["switchboard_status"] = {{"max_per_hour", 1}};
    std::int64_t clock = 1800000000;
    Runtime limited(budget, std::make_unique<FakeHttp>(),
                    [&]
                    {
                        return clock;
                    });
    expect(limited.call("switchboard_status", Json::object())["status"] == "ok", "native initial budget missing");
    expect(limited.call("switchboard_status", Json::object())["status"] == "denied", "native budget was exceeded");
    clock += 3601;
    expect(limited.call("switchboard_status", Json::object())["status"] == "ok", "native rolling budget did not expire");
}

void protocol_and_exposure()
{
    Directory directory;
    auto configuration = test::configuration(directory, {});
    configuration["mcp"]["expose"] = Json::array();
    Runtime runtime(configuration, std::make_unique<FakeHttp>());
    expect(runtime.catalog()["tools"].empty(), "native empty exposure did not hide tools");
    expect(runtime.call("switchboard_status", Json::object())["status"] == "invalid", "native unexposed direct assistant call ran");
    const auto reply = runtime.rpc(
        {{"jsonrpc", "2.0"}, {"id", 1}, {"method", "tools/list"}, {"params", {{"_meta", {{"io.modelcontextprotocol/protocolVersion", "2026-07-28"}}}}}});
    expect(reply["result"]["tools"].empty(), "native protocol exposure changed");
    expect(reply["result"]["resultType"] == "complete", "native modern result shape changed");
    const auto failed = runtime.rpc({{"jsonrpc", "2.0"},
                                     {"id", 2},
                                     {"method", "tools/call"},
                                     {"params", {{"name", "switchboard_status"}, {"_meta", {{"io.modelcontextprotocol/protocolVersion", "2026-07-28"}}}}}});
    expect(failed["error"]["code"] == -32602, "native protocol invoked an unexposed tool");
}

void audit_chain()
{
    Directory directory;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>());
    expect(runtime.call("switchboard_status", Json::object())["status"] == "ok", "native status failed");
    expect(runtime.call("audit_verify", Json::object())["data"]["chain_matches"] == true, "native audit chain failed");
    Database editor(directory.path / "native-state.sqlite3");
    editor.execute("UPDATE native_audit SET record = '{}' WHERE id = 1");
    expect(runtime.call("audit_verify", Json::object())["status"] == "error", "native audit edit was not detected");
}
}  // namespace

int main()
{
    schema_and_json();
    dates_and_private_files();
    approval_revalidation_and_budget();
    protocol_and_exposure();
    audit_chain();
    return 0;
}

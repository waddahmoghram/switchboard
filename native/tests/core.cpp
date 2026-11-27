#include "support.hpp"

#include <cmath>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <sys/stat.h>

using namespace switchboard;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::expect_error_code;
using switchboard::test::FakeHttp;
using switchboard::test::ManualClock;

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

void runtime_settings()
{
    Directory directory;
    auto configuration = test::configuration(directory, {});
    configuration["switchboard"]["tool_timeout_seconds"] = 5;
    configuration["switchboard"]["job_timeout_seconds"] = 3600;
    configuration["switchboard"]["tick_budget_seconds"] = 30;
    configuration["retention"] = {{"tool_calls_days", 1}, {"approvals_days", 3650}, {"actions_days", 180}};
    configuration["broker"] = {{"client_socket", "/run/switchboard/client.sock"},
                               {"operator_socket", "/run/switchboard/operator.sock"},
                               {"client_uids", Json::array({1001})},
                               {"operator_uids", Json::array({0, 4294967294LL})}};
    Runtime accepted(configuration, std::make_unique<FakeHttp>());
    expect(accepted.call("switchboard_status", Json::object())["status"] == "ok", "native bounded settings were not accepted");
    const auto rejected = [&](const char *section, const char *key, const Json &value, const std::string &code)
    {
        auto changed = configuration;
        changed[section][key] = value;
        expect_error_code(
            [&]
            {
                Runtime runtime(changed, std::make_unique<FakeHttp>());
            },
            code, std::string("native setting was accepted: ") + section + "." + key + "=" + value.dump());
    };
    rejected("switchboard", "tool_timeout_seconds", 4, "invalid");
    rejected("switchboard", "tool_timeout_seconds", 3601, "invalid");
    rejected("switchboard", "job_timeout_seconds", 4, "invalid");
    rejected("switchboard", "job_timeout_seconds", 3601, "invalid");
    rejected("switchboard", "tick_budget_seconds", 29, "invalid");
    rejected("switchboard", "tick_budget_seconds", 3601, "invalid");
    rejected("switchboard", "tick_budget_seconds", 60.5, "invalid");
    rejected("switchboard", "job_workers", 4, "unsupported");
    rejected("retention", "actions_days", 0, "invalid");
    rejected("retention", "approvals_days", 3651, "invalid");
    rejected("retention", "connectors", Json::object(), "unsupported");
    rejected("retention", "events_days", 30, "invalid");
    rejected("broker", "client_uids", Json::array(), "invalid");
    rejected("broker", "client_uids", Json::array({-1}), "invalid");
    rejected("broker", "client_uids", Json::array({4294967295LL}), "invalid");
    rejected("broker", "client_uids", Json::array({true}), "invalid");
    rejected("broker", "operator_uids", Json::array({"1000"}), "invalid");
    rejected("broker", "operator_uids", Json(std::vector<int>(65, 1000)), "invalid");
    rejected("broker", "client_socket", "", "invalid");
    rejected("broker", "client_socket", "   ", "invalid");
    rejected("broker", "operator_socket", 5, "invalid");
    rejected("broker", "listen_address", "0.0.0.0", "invalid");
    rejected("audit", "hash_only", "message", "invalid");
    rejected("audit", "hash_only", Json::array({""}), "invalid");
    rejected("audit", "hash_only", Json::array({" message"}), "invalid");
    rejected("audit", "hash_only", Json::array({"message\xC2\xA0"}), "invalid");
    rejected("audit", "hash_only", Json::array({5}), "invalid");
    rejected("audit", "hash_only", Json(std::vector<std::string>(65, "message")), "invalid");
    rejected("audit", "keep", true, "invalid");
    bool explained = false;
    try
    {
        auto changed = configuration;
        changed["switchboard"]["job_workers"] = 2;
        Runtime runtime(changed, std::make_unique<FakeHttp>());
    }
    catch (const Error &error)
    {
        explained = std::string(error.what()).find("Python-only") != std::string::npos;
    }
    expect(explained, "native job_workers rejection must say that the setting is Python-only");
    auto array_broker = configuration;
    array_broker["broker"] = Json::array();
    expect_error_code(
        [&]
        {
            Runtime runtime(array_broker, std::make_unique<FakeHttp>());
        },
        "invalid", "native broker section must be an object");
    auto array_audit = configuration;
    array_audit["audit"] = Json::array();
    expect_error_code(
        [&]
        {
            Runtime runtime(array_audit, std::make_unique<FakeHttp>());
        },
        "invalid", "native audit section must be an object");
    auto hashed = configuration;
    hashed["audit"] = {{"hash_only", Json(std::vector<std::string>(64, "message"))}};
    Runtime most(hashed, std::make_unique<FakeHttp>());
    expect(most.call("switchboard_status", Json::object())["status"] == "ok", "native audit.hash_only refused 64 names");
}

// Audit records and journal arguments are redacted by the shared rules; only audit records apply audit.hash_only.
void audit_redaction()
{
    Directory directory;
    auto configuration = test::configuration(directory, {});
    configuration["audit"] = {{"hash_only", Json::array({"NOTE", "at"})}};
    const Extension tools = [](Context &, Registry &registry)
    {
        registry.add({"test_send", "Synthetic draft-tier write for audit tests.", Tier::draft,
                      object_schema({{"note", {{"type", "string"}}}, {"password", {{"type", "string"}}}, {"tag", {{"type", "string"}}}}), [](const Json &)
                      {
                          return Json{{"sent", true}};
                      }});
    };
    Runtime runtime(configuration, std::make_unique<FakeHttp>(), {}, tools);
    const auto result = runtime.call("test_send", {{"note", "call me, password: hunter2"}, {"password", "hunter2"}, {"tag", "Bearer abcdefghijklmnop1"}});
    expect(result["status"] == "ok" && result["action"]["id"].is_string(), "native audit test tool failed");
    const auto action = result["action"]["id"].get<std::string>();
    Database editor(directory.path / "native-state.sqlite3");
    Json request;
    for (const auto &row : editor.query("SELECT record FROM native_audit ORDER BY id"))
    {
        auto record = Json::parse(row.at("record").get<std::string>());
        if (record["event"] == "tool.request")
        {
            request = std::move(record);
        }
    }
    expect(request["arguments"] == Json{{"note", "[29 chars, sha256:b388afd740d0981b]"}, {"password", "[redacted]"}, {"tag", "Bearer [redacted]"}},
           "native audit record arguments were not redacted like Python's: " + request.dump());
    expect(request["action"] == action, "native audit record lost the action identifier under an identifier key");
    expect(request["at"].is_string() && request["at"].get<std::string>().find("chars, sha256:") == std::string::npos,
           "native audit record hashed its own time");
    const auto journal = runtime.actions({{"id", action}});
    expect(journal["action"]["args"] == Json{{"note", "call me, password: [redacted]"}, {"password", "[redacted]"}, {"tag", "Bearer [redacted]"}},
           "native journal arguments must be scrubbed without audit.hash_only: " + journal.dump());
    expect(runtime.call("audit_verify", Json::object())["data"]["chain_matches"] == true, "native redacted audit chain failed");
}

void journal_tool_and_status()
{
    Directory directory;
    test::SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    bool listed = false;
    const auto catalog = runtime.catalog();
    for (const auto &tool : catalog.at("tools"))
    {
        if (tool["name"] == "actions_recent")
        {
            listed = tool["annotations"]["readOnlyHint"] == true;
        }
        expect(tool["name"].get<std::string>().find("reconcile") == std::string::npos, "assistants must not receive a reconciliation tool");
    }
    expect(listed, "native action journal tool is missing or not read-only");
    const auto status = runtime.call("switchboard_status", Json::object());
    expect(status["data"]["open_actions"] == Json{{"in_flight", 0}, {"uncertain", 0}} && status["data"]["schema_version"] == STATE_SCHEMA_VERSION &&
               !status.contains("action"),
           "native status must report open journal counts and must not journal reads");
    const auto unknown = runtime.call("synthetic_write", {{"mode", "effect_unknown"}});
    expect(unknown["status"] == "ok" && unknown["action"]["state"] == "uncertain", "a returned effect_unknown report must be uncertain");
    const auto recent = runtime.call("actions_recent", Json::object());
    expect(recent["status"] == "ok" && recent["data"]["actions"].size() == 1 && !recent["data"]["actions"][0].contains("args") &&
               !recent["data"]["actions"][0].contains("owner") && recent["data"]["actions"][0]["started_at"].is_string() &&
               recent["data"]["open"]["uncertain"] == 1,
           "the assistant journal view must omit arguments and owners");
    expect(runtime.call("actions_recent", {{"state", "succeeded"}})["data"]["actions"].empty(), "native journal state filter failed");
    expect(runtime.call("actions_recent", {{"state", "unknown"}})["status"] == "invalid", "native journal accepted an unknown state");
    expect(runtime.call("actions_recent", {{"limit", 101}})["status"] == "invalid", "native journal accepted an unbounded limit");
    expect(runtime.call("switchboard_status", Json::object())["data"]["open_actions"]["uncertain"] == 1, "native status missed an uncertain action");
    const auto reply = runtime.rpc({{"jsonrpc", "2.0"},
                                    {"id", 7},
                                    {"method", "tools/call"},
                                    {"params",
                                     {{"name", "synthetic_write"},
                                      {"arguments", {{"mode", "effect_unknown"}, {"tag", "protocol"}}},
                                      {"_meta", {{"io.modelcontextprotocol/protocolVersion", "2026-07-28"}}}}}});
    expect(reply["result"]["isError"] == true, "native protocol must flag a returned report whose effect is unknown");
}

void bounded_tick_and_backoff()
{
    Directory directory;
    ManualClock clock;
    const auto start = clock.now();
    auto configuration = test::configuration(directory, {});
    configuration["switchboard"]["tick_budget_seconds"] = 30;
    configuration["switchboard"]["job_timeout_seconds"] = 5;
    std::vector<std::string> order;
    bool slow_first = true;
    bool flaky_fails = true;
    std::optional<ExecutionLimits> observed;
    const Extension jobs = [&](Context &context, Registry &)
    {
        context.add_job("test.slow", 60,
                        [&]
                        {
                            order.push_back("slow");
                            if (slow_first)
                            {
                                slow_first = false;
                                clock.advance(31);
                            }
                        });
        context.jobs.back().next_run = start - 100;
        context.add_job("test.flaky", 60,
                        [&]
                        {
                            order.push_back("flaky");
                            observed = current_execution_limits();
                            if (flaky_fails)
                            {
                                throw Error("test_failure", "synthetic job failure");
                            }
                        });
        context.jobs.back().next_run = start - 50;
        context.add_job("test.later", 60,
                        [&]
                        {
                            order.push_back("later");
                        });
    };
    Runtime runtime(configuration, std::make_unique<FakeHttp>(), clock.clock(), jobs);

    const auto first = runtime.tick();
    expect(order == std::vector<std::string>{"slow"} && first["budget_exhausted"] == true && first["deferred"] == Json::array({"test.flaky", "test.later"}) &&
               first["jobs"][0]["overran"] == true,
           "native tick must run the most overdue job first and stop at its budget");
    const auto second = runtime.tick();
    expect(order == std::vector<std::string>{"slow", "flaky", "later"} && second["deferred"].empty() && second["budget_exhausted"] == false,
           "native jobs left by an exhausted budget must stay due");
    expect(observed && observed->remaining_seconds() > 0 && observed->remaining_seconds() <= 5 && !observed->cancelled(),
           "native jobs must run under their own deadline");
    expect(!current_execution_limits().has_value(), "native job limits must not leak out of a tick");
    expect(second["jobs"][0]["error"] == "test_failure" && second["jobs"][0]["consecutive_failures"] == 1 && second["jobs"][0]["next_run"] == start + 31 + 60,
           "a first failure must keep the normal interval");

    clock.advance(60);
    const auto third = runtime.tick();
    expect(order.size() == 6 && order[3] == "slow" && order[4] == "flaky" && order[5] == "later", "equally overdue jobs must keep registration order");
    expect(third["jobs"][1]["consecutive_failures"] == 2 && third["jobs"][1]["next_run"] == start + 91 + 120, "repeated failures must back off exponentially");

    flaky_fails = false;
    clock.advance(120);
    (void)runtime.tick();
    expect(order.size() == 9 && order[6] == "slow" && order[7] == "later" && order[8] == "flaky", "the least overdue job must run last");
    Json flaky;
    const auto jobs_report = runtime.call("jobs_status", Json::object());
    for (const auto &job : jobs_report.at("data"))
    {
        if (job["name"] == "test.flaky")
        {
            flaky = job;
        }
    }
    expect(flaky["consecutive_failures"] == 0 && flaky["error"].is_null() && flaky["next_run"] == start + 211 + 60 && flaky["overruns"] == 0 &&
               flaky["last_duration_ms"].is_number_integer() && flaky["last_started_at"] == start + 211 && flaky["timeout_seconds"] == 5,
           "a success must reset backoff and report the last run");
}

void tick_cancellation()
{
    Directory directory;
    Runtime *active = nullptr;
    bool cancelled_inside = false;
    bool second_cancelled = true;
    int second_runs = 0;
    const Extension jobs = [&](Context &context, Registry &)
    {
        context.add_job("test.first", 60,
                        [&]
                        {
                            active->cancel();
                            cancelled_inside = current_execution_limits()->cancelled();
                        });
        context.jobs.back().next_run -= 10;
        context.add_job("test.second", 60,
                        [&]
                        {
                            ++second_runs;
                            second_cancelled = current_execution_limits()->cancelled();
                        });
    };
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, jobs);
    active = &runtime;
    const auto first = runtime.tick();
    expect(cancelled_inside && first["cancelled"] == true && first["deferred"] == Json::array({"test.second"}) && second_runs == 0,
           "a cancellation during a tick must reach the running job and defer the rest");
    const auto second = runtime.tick();
    expect(second["cancelled"] == false && second_runs == 1 && !second_cancelled, "a cancellation must not affect a later tick");
}
}  // namespace

int main()
{
    schema_and_json();
    dates_and_private_files();
    approval_revalidation_and_budget();
    protocol_and_exposure();
    audit_chain();
    runtime_settings();
    audit_redaction();
    journal_tool_and_status();
    bounded_tick_and_backoff();
    tick_cancellation();
    return 0;
}

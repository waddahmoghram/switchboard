#include "../src/broker_internal.hpp"
#include "support.hpp"

#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <sys/socket.h>
#include <unistd.h>

using namespace switchboard;
using switchboard::broker::LineBuffer;
using switchboard::broker::Role;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::FakeHttp;

namespace
{
template <typename Function> void rejects(Function function, const std::string &message)
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
    expect(rejected, message);
}

Json reply(const std::optional<std::string> &line)
{
    expect(line.has_value() && !line->empty() && line->back() == '\n', "native broker response is not one framed line");
    expect(line->find('\n') == line->size() - 1, "native broker response contains an embedded line break");
    return parse_json(line->substr(0, line->size() - 1), broker::MAX_RESPONSE_BYTES);
}

BrokerOptions options()
{
    BrokerOptions result;
    result.client_socket = "/run/switchboard-native/client.sock";
    result.operator_socket = "/run/switchboard-native/operator.sock";
    result.client_uids = {1001};
    result.operator_uids = {1000};
    return result;
}

void line_framing()
{
    LineBuffer lines(8);
    expect(lines.append("one\ntwo\nthr") && lines.ready(), "complete native broker lines were not recognized");
    expect(lines.next() == "one" && lines.next() == "two", "native broker lines were split incorrectly");
    expect(!lines.next() && !lines.ready(), "an unterminated native broker line was released early");
    expect(lines.append("ee\n\n") && lines.next() == "three", "a line split across reads was not joined");
    expect(lines.next() == std::string(), "an empty line was not returned for the caller to skip");
    expect(!lines.finish(), "finish returned data although no unterminated line remained");
    expect(lines.append("tail") && !lines.next() && lines.finish() == "tail" && !lines.finish(), "the unterminated final line was not returned once");

    LineBuffer exact(8);
    expect(exact.append("12345678\n") && exact.next() == "12345678", "a line of exactly the bound was refused");
    expect(exact.append("12345678"), "an unterminated line at the bound was refused");
    expect(!exact.append("9") && exact.overflowed(), "an unterminated line beyond the bound was accepted");
    expect(!exact.next() && !exact.finish() && !exact.append("\n") && !exact.ready(), "an overflowed line buffer recovered");

    LineBuffer complete(8);
    expect(!complete.append("123456789\nok\n") && !complete.ready(), "a complete line beyond the bound was accepted");

    LineBuffer trickle(8);
    for (const char byte : std::string("abc\ndef\n"))
    {
        expect(trickle.append(std::string_view(&byte, 1)), "byte-by-byte input was refused");
    }
    expect(trickle.next() == "abc" && trickle.next() == "def" && !trickle.next(), "byte-by-byte input was framed differently");

    LineBuffer standard;
    expect(standard.append(std::string(broker::MAX_LINE_BYTES, 'x')), "the default bound refused a 1 MiB line");
    expect(!standard.append("x"), "the default bound accepted a line over 1 MiB");

    broker::LineMeter meter(4);
    expect(meter.add("1234\n12") && meter.add("34\n"), "the line meter refused lines within its bound");
    expect(!meter.add("12345") && !meter.add("\n"), "the line meter accepted or forgot an overlong line");
}

void response_framing()
{
    const auto small = reply(broker::frame(Json{{"jsonrpc", "2.0"}, {"id", 7}, {"result", Json::object()}}, Role::client));
    expect(small["id"] == 7 && small.contains("result"), "a small native broker response changed");

    const auto large = Json{{"jsonrpc", "2.0"}, {"id", 7}, {"result", {{"text", std::string(200, 'x')}}}};
    const auto replaced = reply(broker::frame(large, Role::client, 160));
    expect(replaced["id"] == 7 && replaced["error"]["code"] == -32603 && !replaced.contains("result"), "an oversized client response was not replaced");
    const auto operator_large = reply(broker::frame(Json{{"ok", true}, {"op", "actions"}, {"result", std::string(200, 'x')}}, Role::operator_, 160));
    expect(operator_large["ok"] == false && operator_large["op"] == "actions" && operator_large["error"]["code"] == "limit",
           "an oversized operator response was not replaced");

    expect(reply(broker::overflow_response(Role::client))["error"]["code"] == -32600, "the client overflow response differs");
    expect(reply(broker::overflow_response(Role::operator_))["error"]["code"] == "limit", "the operator overflow response differs");
    expect(broker::frame(Json{{"text", std::string("\xff")}}, Role::operator_).back() == '\n', "text that is not UTF-8 broke framing");
}

void authorization()
{
    const auto configured = options();
    expect(broker::authorized(Role::client, 1001U, configured), "a listed client account was refused");
    expect(!broker::authorized(Role::operator_, 1001U, configured), "a client account reached the operator socket");
    expect(broker::authorized(Role::operator_, 1000U, configured), "a listed operator account was refused");
    expect(!broker::authorized(Role::client, 1000U, configured), "an account not listed for the client socket reached it");
    expect(!broker::authorized(Role::client, std::nullopt, configured) && !broker::authorized(Role::operator_, std::nullopt, configured),
           "a peer without credentials was admitted");
    expect(!broker::authorized(Role::client, 0U, configured), "root was admitted without being listed");
    expect(!broker::authorized(Role::client, 4294967295U, configured), "the reserved user identifier was admitted");

    broker::check_options(configured);
    const auto invalid = [&configured](auto change)
    {
        auto changed = configured;
        change(changed);
        rejects(
            [&changed]
            {
                broker::check_options(changed);
            },
            "invalid native broker options were accepted");
    };
    invalid(
        [](BrokerOptions &value)
        {
            value.client_socket = "relative/client.sock";
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.operator_socket = value.client_socket;
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.client_socket = "/run/switchboard-native/../client.sock";
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.client_socket = "/run/switchboard-native/";
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.client_socket = "/" + std::string(200, 'x');
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.client_uids.clear();
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.operator_uids = std::vector<std::uint32_t>(65, 1000);
        });
    invalid(
        [](BrokerOptions &value)
        {
            value.operator_uids = {4294967295U};
        });
}

void peer_credentials()
{
    expect(!broker::peer_uid(-1), "an invalid descriptor produced peer credentials");
    if (!broker::peer_credentials_supported())
    {
        return;
    }
    // An anonymous connected pair needs no address, listening socket or second account.
    std::array<int, 2> ends{-1, -1};
    expect(socketpair(AF_UNIX, SOCK_STREAM, 0, ends.data()) == 0, "a local socket pair could not be created");
    const auto uid = broker::peer_uid(ends[0]);
    close(ends[0]);
    close(ends[1]);
    expect(uid && *uid == static_cast<std::uint32_t>(geteuid()), "the peer-credential facility did not report this process's account");
}

// Injected roles stand in for peers whose credentials were already checked: no line from the client socket can
// reach an operator method, while the operator socket can.
void operator_separation()
{
    Directory directory;
    int executions = 0;
    Runtime runtime(switchboard::test::configuration(directory, {}), std::make_unique<FakeHttp>(), {},
                    [&executions](Context &, Registry &registry)
                    {
                        registry.add({"test_switch", "Synthetic act-tier tool for broker separation tests.", Tier::act, object_schema(Json::object()),
                                      [&executions](const Json &)
                                      {
                                          ++executions;
                                          return Json{{"confirmed", true}};
                                      }});
                    });
    const auto pending = runtime.call("test_switch", Json::object(), Origin::assistant);
    expect(pending["status"] == "pending", "the synthetic action was not queued for approval");
    const auto id = pending["approval_id"].get<std::string>();
    const auto approval_state = [&runtime, &id]
    {
        return runtime.call("approvals_status", {{"id", id}}, Origin::human)["data"]["status"];
    };
    const auto kill_switch_state = [&runtime]
    {
        return runtime.call("switchboard_status", Json::object(), Origin::human)["data"]["kill_switch"];
    };

    const auto approve = Json{{"op", "approve"}, {"id", id}}.dump();
    const auto refused = reply(broker::respond(runtime, Role::client, approve));
    expect(refused["error"]["code"] == -32600 && !refused.contains("result"), "the client socket accepted an operator request");
    for (const std::string &line : {Json{{"op", "deny"}, {"id", id}}.dump(), std::string("{\"op\":\"kill_switch\",\"enable\":true}"),
                                    std::string("{\"op\":\"reconcile\",\"id\":\"00000000000000000000000000000000\",\"resolution\":\"applied\",\"note\":\"x\"}"),
                                    std::string("{\"op\":\"approvals\"}"), Json{{"op", "approval"}, {"id", id}}.dump()})
    {
        expect(reply(broker::respond(runtime, Role::client, line)).contains("error"), "the client socket answered an operator request");
    }
    expect(executions == 0 && approval_state() == "pending", "an approval was decided from the client socket");
    expect(kill_switch_state() == false, "the client socket toggled the kill switch");

    // Through the assistant protocol, the act tool can only be queued again.
    expect(reply(broker::respond(runtime, Role::client, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{}}")).contains("result"),
           "the client socket did not answer initialize");
    const auto queued = reply(broker::respond(
        runtime, Role::client, "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"test_switch\",\"arguments\":{}}}"));
    expect(queued["id"] == 2 && queued["result"]["content"][0]["text"].get<std::string>().find("\"pending\"") != std::string::npos && executions == 0,
           "an assistant protocol call executed without approval");
    expect(!broker::respond(runtime, Role::client, "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}"), "a notification received a response");
    expect(!broker::respond(runtime, Role::client, " \r") && !broker::respond(runtime, Role::operator_, ""), "a blank line received a response");
    const auto unparsable = reply(broker::respond(runtime, Role::client, "{"));
    expect(unparsable["error"]["code"] == -32700 && unparsable["id"].is_null(), "an unparsable client line was not a parse error");

    const auto protocol = reply(broker::respond(runtime, Role::operator_, "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\"}"));
    expect(protocol["ok"] == false && protocol["error"]["code"] == "invalid", "the operator socket answered the assistant protocol");
    for (const std::string line : {"[]", "{\"op\":\"launch\"}", "{\"op\":\"approve\"}", "{\"op\":\"approve\",\"id\":\"x\",\"extra\":true}",
                                   "{\"op\":\"kill_switch\",\"enable\":\"yes\"}", "{\"op\":\"status\",\"verbose\":true}", "{\"op\":7}", "not json",
                                   "{\"op\":\"approval\"}", "{\"op\":\"actions\",\"limit\":\"10\"}", "{\"op\":\"actions\",\"state\":3}"})
    {
        const auto failure = reply(broker::respond(runtime, Role::operator_, line));
        expect(failure["ok"] == false && failure["error"]["code"] == "invalid", "a malformed operator request was accepted");
    }

    const auto tagged = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"status\",\"request_id\":\"r-1\"}"));
    expect(tagged["ok"] == true && tagged["request_id"] == "r-1", "the operator request_id was not echoed");
    const auto untagged = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"status\",\"request_id\":[1]}"));
    expect(untagged["ok"] == false && untagged["request_id"].is_null() && untagged["error"]["code"] == "invalid",
           "an invalid operator request_id was accepted or echoed");

    const auto review = reply(broker::respond(runtime, Role::operator_, Json{{"op", "approval"}, {"id", id}}.dump()));
    expect(review["ok"] == true && review["op"] == "approval" && review["result"]["id"] == id && review["result"]["tool"] == "test_switch" &&
               review["result"]["status"] == "pending" && review["result"]["args"] == Json::object() && executions == 0,
           "the operator could not review the exact stored arguments before approving");
    for (const auto &unknown : {std::string("x"), std::string(32, '0')})
    {
        const auto missing = reply(broker::respond(runtime, Role::operator_, Json{{"op", "approval"}, {"id", unknown}}.dump()));
        expect(missing["ok"] == false && missing["op"] == "approval" && missing["error"]["code"] == "invalid",
               "a malformed or unknown approval was not an error response");
    }

    const auto approved = reply(broker::respond(runtime, Role::operator_, approve));
    expect(approved["ok"] == true && approved["op"] == "approve" && approved["result"]["status"] == "ok" && executions == 1,
           "operator approval did not execute exactly once");
    const auto replay = reply(broker::respond(runtime, Role::operator_, approve));
    expect(replay["ok"] == true && replay["result"]["status"] != "ok" && executions == 1, "a decided approval executed again");

    expect(reply(broker::respond(runtime, Role::operator_, "{\"op\":\"kill_switch\",\"enable\":true}"))["ok"] == true,
           "the operator could not set the kill switch");
    const auto status = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"status\"}"));
    const auto &engine = status["result"]["data"];
    expect(status["ok"] == true && engine["kill_switch"] == true, "operator status does not show the kill switch");
    expect(engine["backend"] == "cpp-native" && engine.contains("version") && engine["pending_approvals"] == 1 && engine["open_actions"]["in_flight"] == 0 &&
               engine["open_actions"]["uncertain"] == 0,
           "operator status lacks the engine, pending approvals or open journal entries");
    const auto blocked = reply(broker::respond(
        runtime, Role::client, "{\"jsonrpc\":\"2.0\",\"id\":4,\"method\":\"tools/call\",\"params\":{\"name\":\"test_switch\",\"arguments\":{}}}"));
    expect(blocked["result"]["isError"] == true && executions == 1, "the kill switch did not deny an assistant action");
    expect(reply(broker::respond(runtime, Role::operator_, "{\"op\":\"kill_switch\",\"enable\":false}"))["ok"] == true,
           "the operator could not clear the kill switch");
    expect(reply(broker::respond(runtime, Role::operator_, "{\"op\":\"kill_switch\"}"))["ok"] == true && kill_switch_state() == false,
           "the operator could not report the kill switch");

    const auto approvals = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"approvals\"}"));
    expect(approvals["ok"] == true && approvals["result"]["data"].size() == 1, "the operator does not see the queued approval");
    const auto second = approvals["result"]["data"][0]["id"].get<std::string>();
    const auto denied = reply(broker::respond(runtime, Role::operator_, Json{{"op", "deny"}, {"id", second}}.dump()));
    expect(denied["ok"] == true && denied["result"]["status"] == "denied" && executions == 1, "the operator could not deny an approval");

    const auto action = approved["result"]["action"]["id"].get<std::string>();
    const auto journal = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"actions\",\"limit\":10}"));
    expect(journal["ok"] == true && journal.dump().find(action) != std::string::npos, "the operator journal lacks the approved action");
    const auto tagged_journal = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"actions\",\"request_id\":7}"));
    expect(tagged_journal["ok"] == true && tagged_journal["request_id"] == 7 && tagged_journal["result"]["status"] == "ok",
           "a request_id changed the journal filter");
    const auto entry = reply(broker::respond(runtime, Role::operator_, Json{{"op", "actions"}, {"id", action}}.dump()));
    expect(entry["ok"] == true && entry["result"]["action"]["id"] == action, "the operator could not read one journal entry");
    for (const auto &refused_filter :
         {Json{{"op", "actions"}, {"limit", 101}}, Json{{"op", "actions"}, {"limit", 0}}, Json{{"op", "actions"}, {"id", action}, {"state", "succeeded"}}})
    {
        const auto listing = reply(broker::respond(runtime, Role::operator_, refused_filter.dump()));
        expect(listing["ok"] == true && listing["result"]["status"] == "invalid", "a journal filter outside the shared protocol was listed");
    }
    const auto reconcile =
        reply(broker::respond(runtime, Role::operator_, Json{{"op", "reconcile"}, {"id", action}, {"resolution", "applied"}, {"note", "checked"}}.dump()));
    expect(reconcile.dump().find("reconciled_applied") == std::string::npos, "a succeeded attempt was reconciled");
}
void denied_status_is_an_error()
{
    Directory directory;
    auto configuration = switchboard::test::configuration(directory, {});
    configuration["policy"]["tools"] = {{"switchboard_status", {{"decision", "deny"}}}};
    Runtime runtime(configuration, std::make_unique<FakeHttp>());
    const auto status = reply(broker::respond(runtime, Role::operator_, "{\"op\":\"status\"}"));
    expect(status["ok"] == false && status["error"]["code"] == "unavailable", "a denied engine status was reported as complete");
}
}  // namespace

int main()
{
    line_framing();
    response_framing();
    authorization();
    peer_credentials();
    operator_separation();
    denied_status_is_an_error();
    return 0;
}

#include "support.hpp"

#include <atomic>
#include <chrono>
#include <memory>
#include <optional>
#include <string>
#include <vector>

using namespace switchboard;
using switchboard::test::Directory;
using switchboard::test::expect;
using switchboard::test::expect_error_code;
using switchboard::test::FakeHttp;
using switchboard::test::ManualClock;
using switchboard::test::SyntheticProbe;

namespace
{
const std::string STALE_ID(32, 'a');
const std::string FRESH_ID(32, 'b');
const std::string BOUNDARY_ID(32, 'c');
const std::string RESTART_ID(32, 'd');

std::filesystem::path state_path(const Directory &directory)
{
    return directory.path / "native-state.sqlite3";
}

Json synthetic(const std::string &mode, const std::string &tag)
{
    return {{"mode", mode}, {"tag", tag}};
}

bool identifier(const Json &value)
{
    return value.is_string() && value.get<std::string>().size() == 32 && value.get<std::string>().find_first_not_of("0123456789abcdef") == std::string::npos;
}

bool mentions(const Json &report, const std::string &text)
{
    return report.contains("message") && report["message"].get<std::string>().find(text) != std::string::npos;
}

// A row as another process would leave it: same tool and canonical argument digest, a different owner.
void insert_action(Database &database, const std::string &id, const std::string &state, const Json &arguments, const std::int64_t started,
                   const std::int64_t deadline)
{
    database.execute("INSERT INTO native_actions(id, tool, args_digest, args, origin, approval_id, state, owner, started_at, deadline_at) "
                     "VALUES (?, 'synthetic_write', ?, ?, 'assistant', NULL, ?, 'another-process', ?, ?)",
                     {id, hash(arguments), arguments.dump(-1, ' ', true), state, started, deadline});
}

struct Observation
{
    std::int64_t remaining_seconds = 0;
    bool cancelled_at_start = false;
    bool cancelled_at_end = false;
};

// Behaves like the real transport's contract without any network access: it observes the installed limits,
// can request cancellation mid-transfer, and aborts once the operation is cancelled or past its deadline.
class LimitsTransport final : public HttpTransport
{

public:
    Runtime *runtime = nullptr;
    bool cancel_during_request = false;
    std::vector<Observation> seen;

    HttpResponse request(const HttpRequest &) override
    {
        const auto limits = current_execution_limits();
        if (!limits)
        {
            throw Error("test_route", "web request outside a runtime operation");
        }
        Observation observation;
        observation.remaining_seconds = limits->remaining_seconds();
        observation.cancelled_at_start = limits->cancelled();
        if (cancel_during_request && runtime != nullptr)
        {
            runtime->cancel();
        }
        observation.cancelled_at_end = limits->cancelled();
        seen.push_back(observation);
        if (limits->cancelled())
        {
            throw Error("cancelled", "synthetic transfer aborted by cancellation");
        }
        if (limits->expired())
        {
            throw Error("deadline", "synthetic transfer aborted at its deadline");
        }
        return {200, {}, "{}"};
    }
};

void classification()
{
    Directory directory;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    struct Case
    {
        const char *mode;
        const char *status;
        const char *state;
    };
    const std::vector<Case> cases = {{"ok", "ok", "succeeded"},
                                     {"fail", "error", "failed"},
                                     {"uncertain", "error", "uncertain"},
                                     {"effect_unknown", "ok", "uncertain"},
                                     {"crash", "error", "uncertain"}};
    for (const auto &item : cases)
    {
        const auto report = runtime.call("synthetic_write", synthetic(item.mode, "classification"));
        expect(report["status"] == item.status && report["action"]["state"] == item.state && identifier(report["action"]["id"]),
               std::string("native journal classified mode ") + item.mode + " incorrectly");
    }
    expect(probe.calls == 5, "every allowed write must reach its handler once");
    expect(probe.limits && probe.limits->remaining_seconds() > 0 && probe.limits->remaining_seconds() <= 120, "handlers must run under the tool deadline");
    const auto read = runtime.call("synthetic_read", Json::object());
    expect(read["status"] == "ok" && !read.contains("action"), "read calls must not be journaled");
    const auto pending = runtime.call("synthetic_act", synthetic("ok", "pending"));
    expect(pending["status"] == "pending" && !pending.contains("action"), "pending approvals must not create journal entries");
    const auto invalid = runtime.call("synthetic_write", {{"mode", "unknown"}});
    expect(invalid["status"] == "invalid" && !invalid.contains("action"), "schema failures must not create journal entries");
    expect(probe.calls == 5, "refusals before execution must not run handlers");
    Database inspect(state_path(directory));
    expect(inspect.query("SELECT id FROM native_actions").size() == 5, "each executed non-read attempt must be journaled exactly once");
    const auto failed = inspect.query("SELECT error, origin, owner, finished_at, deadline_at - started_at AS span FROM native_actions WHERE state = 'failed'");
    expect(failed.size() == 1 && failed.front()["error"].get<std::string>().rfind("synthetic_rejected", 0) == 0 && failed.front()["origin"] == "assistant" &&
               failed.front()["owner"].is_string() && failed.front()["finished_at"].is_number_integer() && failed.front()["span"] == 120,
           "a failed attempt must record its error, origin, owner and deadline window");
}

void duplicate_guard_and_reconciliation()
{
    Directory directory;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    const auto uncertain = runtime.call("synthetic_write", synthetic("uncertain", "guard"));
    const auto id = uncertain["action"]["id"].get<std::string>();
    const auto repeated = runtime.call("synthetic_write", synthetic("uncertain", "guard"));
    expect(repeated["status"] == "denied" && !repeated.contains("action") && mentions(repeated, id) && probe.calls == 1,
           "an identical repeat of an uncertain write must be refused and name the blocking action");
    expect(runtime.call("synthetic_write", synthetic("uncertain", "other"))["action"]["state"] == "uncertain" && probe.calls == 2,
           "different arguments must not be blocked");
    for (int attempt = 0; attempt < 2; ++attempt)
    {
        expect(runtime.call("synthetic_write", synthetic("fail", "repeatable"))["action"]["state"] == "failed", "a failed write must be repeatable");
        expect(runtime.call("synthetic_write", synthetic("ok", "repeatable"))["action"]["state"] == "succeeded", "a succeeded write must be repeatable");
    }
    expect(probe.calls == 6, "failed and succeeded attempts must not block identical repeats");

    expect(runtime.reconcile("not-an-identifier", "applied", "checked")["status"] == "invalid", "reconciliation accepted a malformed identifier");
    expect(runtime.reconcile(id, "maybe", "checked")["status"] == "invalid", "reconciliation accepted an unknown resolution");
    expect(runtime.reconcile(id, "applied", "   ")["status"] == "invalid", "reconciliation accepted an empty note");
    expect(runtime.reconcile(id, "applied", "line\nbreak")["status"] == "invalid", "reconciliation accepted a control character");
    expect(runtime.reconcile(id, "applied", std::string(501, 'x'))["status"] == "invalid", "reconciliation accepted an unbounded note");
    expect(runtime.reconcile(std::string(32, 'e'), "applied", "checked")["status"] == "invalid", "reconciliation accepted a missing action");
    const auto succeeded = runtime.call("synthetic_write", synthetic("ok", "settled"));
    expect(runtime.reconcile(succeeded["action"]["id"].get<std::string>(), "applied", "checked")["status"] == "error",
           "only uncertain actions may be reconciled");

    const auto reconciled = runtime.reconcile(id, "applied", "  Checked the device log; the write was applied.  ");
    expect(reconciled["status"] == "ok" && reconciled["action"]["state"] == "reconciled_applied" &&
               reconciled["action"]["resolution_note"] == "Checked the device log; the write was applied." && reconciled["action"]["resolved_at"].is_string() &&
               !reconciled["action"].contains("args"),
           "reconciliation must record the trimmed operator note");
    expect(runtime.reconcile(id, "not_applied", "changed my mind")["status"] == "error", "a reconciled action must not be reconciled again");
    const auto lifted = runtime.call("synthetic_write", synthetic("uncertain", "guard"));
    expect(lifted["action"]["state"] == "uncertain" && lifted["action"]["id"] != id && probe.calls == 8, "reconciliation must lift the duplicate guard");
    const auto second = lifted["action"]["id"].get<std::string>();
    expect(runtime.reconcile(second, "not_applied", "The device still shows the previous state.")["action"]["state"] == "reconciled_not_applied",
           "a not-applied decision must be recorded");

    const auto listed = runtime.actions({{"state", "reconciled_applied"}});
    expect(listed["status"] == "ok" && listed["actions"].size() == 1 && listed["actions"][0]["id"] == id && !listed["actions"][0].contains("args"),
           "operator listings must filter by state without arguments");
    expect(runtime.actions(Json::object())["actions"].size() == 8 && runtime.actions({{"limit", 2}})["actions"].size() == 2,
           "operator listings must honor their limit");
    const auto shown = runtime.actions({{"id", id}});
    expect(shown["status"] == "ok" && shown["action"]["args"] == synthetic("uncertain", "guard") && shown["action"]["owner"].is_string() &&
               shown["action"]["started_at"].is_string(),
           "one operator entry must include its arguments");
    expect(runtime.actions({{"state", "bogus"}})["status"] == "invalid", "operator listing accepted an unknown state");
    expect(runtime.actions({{"limit", 0}})["status"] == "invalid", "operator listing accepted a zero limit");
    expect(runtime.actions({{"id", id}, {"limit", 1}})["status"] == "invalid", "operator lookup accepted mixed filters");
    expect(runtime.actions({{"id", std::string(32, 'f')}})["status"] == "invalid", "operator lookup accepted a missing action");
    expect(runtime.call("reconcile", {{"id", id}})["status"] == "invalid", "assistants must not reach reconciliation");
}

void sweeps_cover_interrupted_owners()
{
    Directory directory;
    ManualClock clock;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), clock.clock(), test::synthetic_tools(probe));
    {
        Database other(state_path(directory));
        insert_action(other, STALE_ID, "in_flight", synthetic("ok", "stale"), clock.now() - 1000, clock.now() - 61);
        insert_action(other, BOUNDARY_ID, "in_flight", synthetic("ok", "boundary"), clock.now() - 1000, clock.now() - 60);
        insert_action(other, FRESH_ID, "in_flight", synthetic("ok", "fresh"), clock.now() - 10, clock.now() + 110);
    }
    const auto blocked = runtime.call("synthetic_write", synthetic("ok", "stale"));
    expect(blocked["status"] == "denied" && mentions(blocked, STALE_ID) && mentions(blocked, "uncertain") && probe.calls == 0,
           "the sweep before the duplicate check must expose an interrupted attempt as uncertain");
    const auto swept = runtime.actions({{"id", STALE_ID}})["action"];
    expect(swept["state"] == "uncertain" && swept["error"].get<std::string>().rfind("interrupted", 0) == 0, "a swept attempt must explain itself");
    expect(runtime.actions({{"id", BOUNDARY_ID}})["action"]["state"] == "in_flight", "an attempt exactly 60 seconds past its deadline must not be swept yet");
    const auto live = runtime.call("synthetic_write", synthetic("ok", "fresh"));
    expect(live["status"] == "denied" && mentions(live, FRESH_ID) && mentions(live, "in flight") &&
               runtime.actions({{"id", FRESH_ID}})["action"]["state"] == "in_flight",
           "a live attempt must block repeats without being swept");

    clock.advance(200);
    const auto ticked = runtime.tick();
    expect(ticked["swept_actions"] == 2 && runtime.actions({{"id", FRESH_ID}})["action"]["state"] == "uncertain", "runner ticks must sweep expired attempts");
    {
        Database other(state_path(directory));
        insert_action(other, RESTART_ID, "in_flight", synthetic("ok", "restart"), clock.now() - 500, clock.now() - 100);
    }
    Runtime restarted(test::configuration(directory, {}), std::make_unique<FakeHttp>(), clock.clock(), test::synthetic_tools(probe));
    expect(restarted.actions({{"id", RESTART_ID}})["action"]["state"] == "uncertain", "an engine start must sweep expired attempts");
    Database inspect(state_path(directory));
    expect(!inspect.query("SELECT id FROM native_audit WHERE record LIKE '%action.swept%'").empty(), "sweeps must be audited");
}

void owner_late_completion()
{
    Directory directory;
    ManualClock clock;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), clock.clock(), test::synthetic_tools(probe));
    // Another process sweeps the attempt while its handler is still running.
    probe.during = [&]
    {
        Database other(state_path(directory));
        other.execute("UPDATE native_actions SET state = 'uncertain', error = 'swept by another process' WHERE state = 'in_flight'");
    };
    const auto late = runtime.call("synthetic_write", synthetic("ok", "late"));
    expect(late["status"] == "ok" && late["action"]["state"] == "succeeded", "the owner must record its late result after a sweep");
    const auto stored = runtime.actions({{"id", late["action"]["id"]}})["action"];
    expect(stored["state"] == "succeeded" && stored["error"].is_null() && stored["finished_at"].is_string(), "the late result must replace the sweep note");
    // An operator decision recorded meanwhile is final; the late result must not overwrite it.
    probe.during = [&]
    {
        Database other(state_path(directory));
        other.execute("UPDATE native_actions SET state = 'reconciled_not_applied', resolved_at = ?, resolution_note = 'checked elsewhere' "
                      "WHERE state = 'in_flight'",
                      {clock.now()});
    };
    const auto overridden = runtime.call("synthetic_write", synthetic("ok", "reconciled"));
    expect(overridden["status"] == "ok" && overridden["action"]["state"] == "reconciled_not_applied",
           "a late result must not overwrite an operator reconciliation");
    probe.during = nullptr;
}

void approvals_link_and_block()
{
    Directory directory;
    SyntheticProbe probe;
    Runtime runtime(test::configuration(directory, {}), std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    const auto pending = runtime.call("synthetic_act", synthetic("ok", "approved"));
    expect(pending["status"] == "pending" && probe.calls == 0, "act-tier extension tools must ask by default");
    const auto approval = pending["approval_id"].get<std::string>();
    const auto review = runtime.approval(approval);
    expect(review["id"] == approval && review["tool"] == "synthetic_act" && review["status"] == "pending" && review["args"] == synthetic("ok", "approved") &&
               review["summary"].is_string() && review["created_at"].is_string() && review["expires_at"].is_string() && review["result"].is_null() &&
               review["error"].is_null() && probe.calls == 0,
           "operator review must show the exact stored arguments without running the handler");
    expect_error_code(
        [&]
        {
            (void)runtime.approval("not-an-identifier");
        },
        "invalid", "approval review must reject a malformed identifier");
    expect_error_code(
        [&]
        {
            (void)runtime.approval(std::string(32, 'e'));
        },
        "invalid", "approval review must reject an unknown identifier");
    const auto executed = runtime.approve(approval);
    expect(executed["status"] == "ok" && executed["action"]["state"] == "succeeded" && probe.calls == 1, "an approval must execute once");
    const auto reviewed = runtime.approval(approval);
    expect(reviewed["status"] == "executed" && reviewed["result"]["action"]["id"] == executed["action"]["id"] && reviewed["error"].is_null(),
           "operator review must show the stored execution result");
    const auto linked = runtime.actions({{"id", executed["action"]["id"]}})["action"];
    expect(linked["approval_id"] == approval && linked["origin"] == "human", "the journal must link the approval");

    const auto direct = runtime.call("synthetic_act", synthetic("uncertain", "blocked"), Origin::human);
    expect(direct["action"]["state"] == "uncertain" && probe.calls == 2, "the blocking attempt must be uncertain");
    const auto blocker = direct["action"]["id"].get<std::string>();
    const auto queued = runtime.call("synthetic_act", synthetic("uncertain", "blocked"));
    expect(queued["status"] == "pending", "queueing an approval runs no handler and no guard");
    const auto blocked = runtime.approve(queued["approval_id"].get<std::string>());
    expect(blocked["status"] == "denied" && mentions(blocked, blocker) && probe.calls == 2, "an approval must pass the duplicate guard when it executes");
    const auto recorded = runtime.call("approvals_status", {{"id", queued["approval_id"]}});
    expect(recorded["data"]["status"] == "failed" && recorded["data"]["error"].get<std::string>().find(blocker) != std::string::npos,
           "a blocked approval must finish as failed with the blocking message");
    const auto failed = runtime.approval(queued["approval_id"].get<std::string>());
    expect(failed["status"] == "failed" && failed["error"].get<std::string>().find(blocker) != std::string::npos && failed["result"]["status"] == "denied",
           "operator review must show why an approval failed");
    expect(runtime.approve(queued["approval_id"].get<std::string>())["status"] == "error", "a failed approval must not be replayable");
}

void cancellation_and_deadlines()
{
    Directory directory;
    SyntheticProbe probe;
    auto configuration = test::configuration(directory, {});
    configuration["switchboard"]["tool_timeout_seconds"] = 5;
    auto transport = std::make_unique<LimitsTransport>();
    auto *web = transport.get();
    Runtime runtime(configuration, std::move(transport), {}, test::synthetic_tools(probe));
    web->runtime = &runtime;
    expect(!current_execution_limits().has_value(), "no limits may be installed outside an operation");

    const auto first = runtime.call("synthetic_write", synthetic("web", "first"));
    expect(first["action"]["state"] == "succeeded" && web->seen.size() == 1 && web->seen[0].remaining_seconds >= 1 && web->seen[0].remaining_seconds <= 5 &&
               !web->seen[0].cancelled_at_start,
           "web requests must see the configured tool deadline");
    web->cancel_during_request = true;
    const auto cancelled = runtime.call("synthetic_write", synthetic("web", "cancelled"));
    expect(cancelled["status"] == "error" && cancelled["action"]["state"] == "uncertain" && !web->seen[1].cancelled_at_start && web->seen[1].cancelled_at_end,
           "a write cancelled mid-transfer must be uncertain");
    web->cancel_during_request = false;
    const auto later = runtime.call("synthetic_write", synthetic("web", "later"));
    expect(later["action"]["state"] == "succeeded" && !web->seen[2].cancelled_at_start, "a cancellation must not affect later calls");
    runtime.cancel();
    const auto idle = runtime.call("synthetic_write", synthetic("web", "idle"));
    expect(idle["action"]["state"] == "succeeded" && !web->seen[3].cancelled_at_start, "a cancellation with nothing in progress must not affect the next call");
    expect(!current_execution_limits().has_value(), "limits must be restored after each call");

    std::atomic<std::uint64_t> generation{7};
    ExecutionLimits manual;
    manual.deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    manual.cancel_generation = &generation;
    manual.started_generation = 7;
    expect(!manual.cancelled() && !manual.expired() && manual.remaining_seconds() >= 9 && manual.remaining_seconds() <= 10, "fresh limits changed");
    HttpRequest request;
    request.url = "https://192.0.2.1/state";
    {
        const ScopedExecutionLimits outer(manual);
        auto inner = manual;
        inner.deadline = std::chrono::steady_clock::now() - std::chrono::seconds(1);
        {
            const ScopedExecutionLimits nested(inner);
            const auto current = current_execution_limits();
            expect(current && current->expired() && current->remaining_seconds() == 0, "expired limits must report no remaining time");
            // The real transport refuses before libcurl starts; 192.0.2.1 is reserved for documentation and nothing is sent.
            const auto real = make_http_transport();
            expect_error_code(
                [&]
                {
                    (void)real->request(request);
                },
                "deadline", "the web transport must refuse to start after the deadline");
        }
        expect(current_execution_limits() && !current_execution_limits()->expired(), "nested limits must restore the outer limits");
        generation.fetch_add(1);
        expect(current_execution_limits()->cancelled(), "a generation change must cancel the installed limits");
        const auto real = make_http_transport();
        expect_error_code(
            [&]
            {
                (void)real->request(request);
            },
            "cancelled", "the web transport must refuse to start once cancelled");
    }
    expect(!current_execution_limits().has_value(), "the outermost scope must restore the absence of limits");
    ExecutionLimits unbound;
    unbound.deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    expect(!unbound.cancelled(), "limits without a cancellation counter are never cancelled");
}

void extension_tools_pass_the_gate()
{
    Directory directory;
    SyntheticProbe probe;
    auto configuration = test::configuration(directory, {});
    configuration["mcp"]["expose"] = Json::array({"switchboard_status", "synthetic_read"});
    configuration["policy"]["tools"]["synthetic_act"] = {{"decision", "deny"}};
    Runtime runtime(configuration, std::make_unique<FakeHttp>(), {}, test::synthetic_tools(probe));
    expect(runtime.call("synthetic_write", synthetic("ok", "hidden"))["status"] == "invalid", "unexposed extension tools must stay hidden from assistants");
    expect(runtime.call("synthetic_act", synthetic("ok", "denied"), Origin::human)["status"] == "denied", "policy must apply to extension tools");
    expect(runtime.call("synthetic_write", {{"mode", "ok"}, {"extra", true}}, Origin::human)["status"] == "invalid", "extension schemas must be enforced");
    expect(runtime.kill_switch(true)["kill_switch"] == true, "the kill switch did not engage");
    expect(runtime.call("synthetic_write", synthetic("ok", "stopped"), Origin::human)["status"] == "denied" &&
               runtime.call("synthetic_read", Json::object())["status"] == "ok",
           "the kill switch must stop extension writes but not reads");
    expect(probe.calls == 0, "no refused extension call may reach its handler");
    expect_error_code(
        [&]
        {
            Runtime shadowed(configuration, std::make_unique<FakeHttp>(), {},
                             [](Context &, Registry &registry)
                             {
                                 registry.add({"switchboard_status", "Shadow.", Tier::read, object_schema(Json::object()),
                                               [](const Json &)
                                               {
                                                   return Json::object();
                                               }});
                             });
        },
        "schema", "an extension must not replace a built-in tool");
}
}  // namespace

int main()
{
    try
    {
        classification();
        duplicate_guard_and_reconciliation();
        sweeps_cover_interrupted_owners();
        owner_late_completion();
        approvals_link_and_block();
        cancellation_and_deadlines();
        extension_tools_pass_the_gate();
        std::cout << "native recovery tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "native recovery test failure: " << error.what() << '\n';
        return 1;
    }
}

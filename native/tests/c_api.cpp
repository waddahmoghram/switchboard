#include "support.hpp"
#include "switchboard/switchboard.h"

#include <array>
#include <cstdint>
#include <cstring>
#include <string>
#include <thread>

using switchboard::Json;
using switchboard::test::Directory;
using switchboard::test::expect;

namespace
{
const std::string STATUS_REQUEST = "{\"tool\":\"switchboard_status\",\"arguments\":{}}";

const uint8_t *bytes(const std::string &text)
{
    return reinterpret_cast<const uint8_t *>(text.data());
}

std::string take(sb_buffer &buffer)
{
    const std::string result = buffer.length == 0 ? "" : std::string(reinterpret_cast<const char *>(buffer.data), buffer.length);
    sb_buffer_free(&buffer);
    expect(buffer.data == nullptr && buffer.length == 0, "native C buffer did not reset");
    return result;
}

sb_runtime *create(const Json &configuration)
{
    sb_runtime *runtime = nullptr;
    sb_buffer output{nullptr, 0};
    const auto text = configuration.dump();
    expect(sb_runtime_create(bytes(text), text.size(), &runtime, &output) == SB_OK, "native C runtime creation failed");
    expect(runtime != nullptr && output.data == nullptr, "native C creation ownership differs");
    return runtime;
}

bool identifier(const char (&text)[33])
{
    if (std::memchr(text, '\0', sizeof(text)) == nullptr || std::strlen(text) != 32)
    {
        return false;
    }
    for (std::size_t index = 0; index < 32; ++index)
    {
        if (!((text[index] >= '0' && text[index] <= '9') || (text[index] >= 'a' && text[index] <= 'f')))
        {
            return false;
        }
    }
    return true;
}

struct Typed
{
    sb_status status;
    sb_outcome outcome;
    std::string report;
};

// Starts from deliberately invalid outcome bytes to prove the library initializes every field.
Typed call_outcome(sb_runtime *runtime, const std::string &request, const sb_origin origin = SB_ASSISTANT)
{
    Typed result{SB_OK, {}, {}};
    std::memset(&result.outcome, 0x7f, sizeof(result.outcome));
    sb_buffer report{nullptr, 0};
    result.status = sb_runtime_call_outcome(runtime, bytes(request), request.size(), origin, &result.outcome, &report);
    result.report = take(report);
    return result;
}

bool no_identifiers(const sb_outcome &outcome)
{
    return outcome.approval_id[0] == '\0' && outcome.action_id[0] == '\0';
}

void original_interface()
{
    static_assert(sizeof(sb_status) == sizeof(std::int32_t));
    static_assert(sizeof(sb_origin) == sizeof(std::int32_t));
    expect(sb_abi_version() == SB_ABI_VERSION, "native C interface version differs");
    sb_runtime *runtime = nullptr;
    sb_buffer output{nullptr, 0};
    const std::string malformed = "{\"unexpected\":true}";
    expect(sb_runtime_create(bytes(malformed), malformed.size(), &runtime, &output) == SB_INVALID_ARGUMENT, "native C accepted invalid configuration");
    expect(runtime == nullptr, "native C published a failed runtime");
    expect(!take(output).empty(), "native C invalid configuration had no error report");

    Directory directory;
    runtime = create(switchboard::test::configuration(directory, {}));
    expect(sb_runtime_catalog(runtime, &output) == SB_OK, "native C catalog failed");
    expect(sb_runtime_catalog(runtime, &output) == SB_INVALID_ARGUMENT, "native C overwrote an owned result");
    expect(take(output).find("switchboard_status") != std::string::npos, "native C catalog lacks core tools");

    expect(sb_runtime_call(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, &output) == SB_OK, "native C core call failed");
    expect(take(output).find("\"python_required\":false") != std::string::npos, "native C call is not a standalone native engine");
    expect(sb_runtime_call(runtime, nullptr, 1, SB_ASSISTANT, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid pointer/count");
    take(output);
    expect(sb_runtime_call(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), 99, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid origin");
    take(output);
    sb_buffer_free(&output);
    sb_runtime_destroy(runtime);
}

void revision_and_layout()
{
    static_assert(SB_ABI_REVISION >= 2U);
    static_assert(sizeof(sb_outcome{}.approval_id) == 33 && sizeof(sb_outcome{}.action_id) == 33);
    static_assert(SB_OUTCOME_OK == 0 && SB_OUTCOME_PENDING == 1 && SB_OUTCOME_DENIED == 2 && SB_OUTCOME_INVALID == 3 && SB_OUTCOME_ERROR == 4);
    static_assert(SB_EFFECT_NONE == 0 && SB_EFFECT_RETURNED == 1 && SB_EFFECT_CONFIRMED == 2 && SB_EFFECT_UNCONFIRMED == 3 && SB_EFFECT_UNKNOWN == 4 &&
                  SB_EFFECT_FAILED == 5);
    expect(sb_abi_revision() == SB_ABI_REVISION, "native C interface revision differs");
}

// A core-only runtime reaches every outcome status that needs no executed write: policy "ask" queues even a
// read tool, and policy "deny" refuses one.
void core_outcomes()
{
    Directory directory;
    auto configuration = switchboard::test::configuration(directory, {});
    configuration["policy"]["tools"] = {{"events_recent", {{"decision", "ask"}}}, {"jobs_status", {{"decision", "deny"}}}};
    auto *runtime = create(configuration);

    const auto read = call_outcome(runtime, STATUS_REQUEST);
    expect(read.status == SB_OK && read.outcome.status == SB_OUTCOME_OK && read.outcome.effect == SB_EFFECT_NONE && no_identifiers(read.outcome),
           "native C read outcome is not a plain report without effect");
    expect(read.report.find("\"tool\":\"switchboard_status\"") != std::string::npos && read.report.find("\"action\"") == std::string::npos,
           "native C read report differs or claims a journal entry");

    const auto pending = call_outcome(runtime, "{\"tool\":\"events_recent\",\"arguments\":{}}");
    expect(pending.status == SB_OK && pending.outcome.status == SB_OUTCOME_PENDING && pending.outcome.effect == SB_EFFECT_NONE,
           "native C pending outcome differs");
    expect(identifier(pending.outcome.approval_id) && pending.outcome.action_id[0] == '\0', "native C pending outcome identifiers differ");
    expect(pending.report.find(pending.outcome.approval_id) != std::string::npos, "native C typed approval identifier differs from its report");

    // Reviewing an approval shows its exact stored arguments and decides nothing.
    sb_buffer output{nullptr, 0};
    const std::string approval_id = pending.outcome.approval_id;
    expect(sb_runtime_approval(runtime, bytes(approval_id), approval_id.size(), &output) == SB_OK, "native C approval review failed");
    expect(sb_runtime_approval(runtime, bytes(approval_id), approval_id.size(), &output) == SB_INVALID_ARGUMENT, "native C overwrote an owned review");
    const auto review = switchboard::parse_json(take(output));
    expect(review["id"] == approval_id && review["tool"] == "events_recent" && review["status"] == "pending" && review["args"] == Json::object(),
           "native C approval review lacks the exact stored arguments");
    // As with approve and deny, malformed and unknown identifiers are invalid reports, not interface failures.
    for (const std::string &unknown : {std::string("x"), std::string(32, '0')})
    {
        expect(sb_runtime_approval(runtime, bytes(unknown), unknown.size(), &output) == SB_OK, "native C approval review of an unknown id failed");
        expect(switchboard::parse_json(take(output))["status"] == "invalid", "native C unknown approval was not an invalid report");
    }
    expect(sb_runtime_approval(runtime, nullptr, 1, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid approval pointer/count");
    take(output);
    expect(sb_runtime_approval(nullptr, bytes(approval_id), approval_id.size(), &output) == SB_INVALID_ARGUMENT,
           "native C reviewed an approval without a runtime");
    take(output);
    expect(sb_runtime_approval(runtime, bytes(approval_id), approval_id.size(), nullptr) == SB_INVALID_ARGUMENT, "native C accepted a missing review buffer");
    const std::string status_request = Json{{"tool", "approvals_status"}, {"arguments", {{"id", approval_id}}}}.dump();
    expect(sb_runtime_call(runtime, bytes(status_request), status_request.size(), SB_HUMAN, &output) == SB_OK, "native C approval status failed");
    expect(switchboard::parse_json(take(output))["data"]["status"] == "pending", "native C approval review changed the approval");

    const auto denied = call_outcome(runtime, "{\"tool\":\"jobs_status\",\"arguments\":{}}");
    expect(denied.status == SB_OK && denied.outcome.status == SB_OUTCOME_DENIED && denied.outcome.effect == SB_EFFECT_NONE && no_identifiers(denied.outcome),
           "native C denied outcome differs");

    const std::array<std::string, 2> invalid_requests{"{\"tool\":\"not_implemented\",\"arguments\":{}}",
                                                      "{\"tool\":\"audit_verify\",\"arguments\":{\"unexpected\":1}}"};
    for (const auto &request : invalid_requests)
    {
        const auto invalid = call_outcome(runtime, request);
        expect(invalid.status == SB_OK && invalid.outcome.status == SB_OUTCOME_INVALID && invalid.outcome.effect == SB_EFFECT_NONE &&
                   no_identifiers(invalid.outcome),
               "native C invalid outcome differs");
    }
    sb_runtime_destroy(runtime);
}

// Interface failures before the engine runs report SB_OUTCOME_ERROR with no effect and no identifiers.
void outcome_arguments()
{
    Directory directory;
    auto *runtime = create(switchboard::test::configuration(directory, {}));
    const auto malformed = call_outcome(runtime, "{");
    const auto origin = call_outcome(runtime, STATUS_REQUEST, 99);
    const auto missing = call_outcome(nullptr, STATUS_REQUEST);
    for (const auto *failed : {&malformed, &origin, &missing})
    {
        expect(failed->status == SB_INVALID_ARGUMENT && failed->outcome.status == SB_OUTCOME_ERROR && failed->outcome.effect == SB_EFFECT_NONE &&
                   no_identifiers(failed->outcome) && !failed->report.empty(),
               "native C typed failure was not reported as an error without effect");
    }

    sb_buffer report{nullptr, 0};
    expect(sb_runtime_call_outcome(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, nullptr, &report) == SB_INVALID_ARGUMENT,
           "native C accepted a missing outcome destination");
    expect(!take(report).empty(), "native C missing outcome destination had no error report");

    sb_outcome outcome{};
    expect(sb_runtime_call_outcome(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, &outcome, nullptr) == SB_INVALID_ARGUMENT,
           "native C accepted a missing report buffer");
    expect(sb_runtime_call_outcome(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, &outcome, &report) == SB_OK,
           "native C typed call failed");
    const auto *owned = report.data;
    expect(sb_runtime_call_outcome(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, &outcome, &report) == SB_INVALID_ARGUMENT,
           "native C overwrote an owned typed report");
    expect(report.data == owned && outcome.status == SB_OUTCOME_ERROR && outcome.effect == SB_EFFECT_NONE, "native C reuse refusal changed ownership");
    expect(take(report).find("switchboard_status") != std::string::npos, "native C first typed report was lost");
    expect(sb_runtime_call_outcome(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_ASSISTANT, &outcome, &report) == SB_OK &&
               outcome.status == SB_OUTCOME_OK,
           "native C freed report buffer could not be reused");
    take(report);
    sb_runtime_destroy(runtime);
}

// Local task writes contact no service, so they exercise journaled effects without network access.
void journaled_outcomes()
{
    Directory directory;
    auto *runtime = create(switchboard::test::configuration(directory, {"tasks"}));
    const auto added = call_outcome(runtime, "{\"tool\":\"tasks_add\",\"arguments\":{\"title\":\"typed outcome\"}}");
    expect(added.status == SB_OK && added.outcome.status == SB_OUTCOME_OK, "native C local write failed");
    expect(added.outcome.effect == SB_EFFECT_RETURNED || added.outcome.effect == SB_EFFECT_CONFIRMED || added.outcome.effect == SB_EFFECT_UNCONFIRMED,
           "native C completed write must report a returned, confirmed or unconfirmed effect");
    expect(identifier(added.outcome.action_id) && added.outcome.approval_id[0] == '\0', "native C completed write did not name its journal entry");
    expect(added.report.find(added.outcome.action_id) != std::string::npos, "native C typed action identifier differs from its report");

    // The handler refuses an update without fields before writing, so the attempt finishes as failed.
    const auto refused = call_outcome(runtime, "{\"tool\":\"tasks_update\",\"arguments\":{\"id\":1,\"revision\":1}}");
    expect(refused.status == SB_OK && refused.outcome.status == SB_OUTCOME_ERROR && refused.outcome.effect == SB_EFFECT_FAILED &&
               identifier(refused.outcome.action_id),
           "native C refused write is not a failed journal entry");

    // The handler rejects a zero byte in the notes after the attempt was recorded: the report is `invalid`, and
    // the journal entry, checked before the status, makes the effect failed.
    const auto rejected = call_outcome(runtime, "{\"tool\":\"tasks_add\",\"arguments\":{\"title\":\"x\",\"notes\":\"a\\u0000b\"}}");
    expect(rejected.status == SB_OK && rejected.outcome.status == SB_OUTCOME_INVALID && rejected.outcome.effect == SB_EFFECT_FAILED &&
               identifier(rejected.outcome.action_id),
           "native C invalid write with a failed journal entry is not a failed effect");

    sb_buffer output{nullptr, 0};
    expect(sb_runtime_actions(runtime, nullptr, 0, &output) == SB_OK, "native C journal listing failed");
    const auto journal = take(output);
    expect(journal.find(added.outcome.action_id) != std::string::npos && journal.find(refused.outcome.action_id) != std::string::npos,
           "native C journal lacks executed attempts");

    // Only uncertain attempts can be reconciled; a succeeded attempt stays as it was.
    const auto request = Json{{"id", added.outcome.action_id}, {"resolution", "applied"}, {"note", "checked by the authored test"}}.dump();
    const auto status = sb_runtime_reconcile(runtime, bytes(request), request.size(), &output);
    expect(status != SB_OK || take(output).find("reconciled_applied") == std::string::npos, "native C reconciled an attempt that was not uncertain");
    sb_buffer_free(&output);
    sb_runtime_destroy(runtime);
}

void operator_functions()
{
    Directory directory;
    auto *runtime = create(switchboard::test::configuration(directory, {}));
    sb_buffer output{nullptr, 0};

    expect(sb_runtime_actions(runtime, nullptr, 0, &output) == SB_OK, "native C empty action filter failed");
    expect(sb_runtime_actions(runtime, nullptr, 0, &output) == SB_INVALID_ARGUMENT, "native C overwrote an owned journal report");
    take(output);
    const std::string filter = "{\"state\":\"uncertain\",\"limit\":10}";
    expect(sb_runtime_actions(runtime, bytes(filter), filter.size(), &output) == SB_OK, "native C state filter failed");
    take(output);
    const std::string array = "[]";
    expect(sb_runtime_actions(runtime, bytes(array), array.size(), &output) == SB_INVALID_ARGUMENT, "native C accepted a non-object filter");
    take(output);
    expect(sb_runtime_actions(runtime, nullptr, 1, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid filter pointer/count");
    take(output);
    expect(sb_runtime_actions(nullptr, nullptr, 0, &output) == SB_INVALID_ARGUMENT, "native C listed actions without a runtime");
    take(output);
    expect(sb_runtime_actions(runtime, nullptr, 0, nullptr) == SB_INVALID_ARGUMENT, "native C accepted a missing output buffer");

    const std::array<std::string, 5> malformed_reconciles{"{}", "[]", "{\"id\":\"x\",\"resolution\":\"applied\"}",
                                                          "{\"id\":\"x\",\"resolution\":\"applied\",\"note\":\"n\",\"extra\":1}",
                                                          "{\"id\":1,\"resolution\":\"applied\",\"note\":\"n\"}"};
    for (const auto &request : malformed_reconciles)
    {
        expect(sb_runtime_reconcile(runtime, bytes(request), request.size(), &output) == SB_INVALID_ARGUMENT, "native C accepted a malformed reconcile");
        expect(!take(output).empty(), "native C malformed reconcile had no error report");
    }

    for (const int32_t mode : {-2, 2, 99})
    {
        expect(sb_runtime_kill_switch(runtime, mode, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid kill switch mode");
        take(output);
    }
    const auto kill_switch_state = [&]
    {
        expect(sb_runtime_call(runtime, bytes(STATUS_REQUEST), STATUS_REQUEST.size(), SB_HUMAN, &output) == SB_OK, "native C status call failed");
        return switchboard::parse_json(take(output))["data"]["kill_switch"];
    };
    expect(sb_runtime_kill_switch(runtime, 1, &output) == SB_OK, "native C could not turn the kill switch on");
    take(output);
    expect(kill_switch_state() == true, "native C kill switch did not turn on");
    expect(sb_runtime_kill_switch(runtime, -1, &output) == SB_OK, "native C could not report the kill switch");
    take(output);
    expect(sb_runtime_kill_switch(runtime, 0, &output) == SB_OK, "native C could not turn the kill switch off");
    take(output);
    expect(kill_switch_state() == false, "native C kill switch did not turn off");

    for (const int32_t dry_run : {-1, 2})
    {
        expect(sb_runtime_purge(runtime, dry_run, &output) == SB_INVALID_ARGUMENT, "native C accepted an ambiguous purge mode");
        take(output);
    }
    expect(sb_runtime_purge(runtime, 1, &output) == SB_OK, "native C purge dry run failed");
    take(output);

    const std::string with_nul("/bad\0path", 9);
    const std::string not_utf8 = "/bad\xff";
    expect(sb_runtime_backup(runtime, nullptr, 0, &output) == SB_INVALID_ARGUMENT, "native C accepted an empty backup path");
    take(output);
    for (const auto *path : {&with_nul, &not_utf8})
    {
        expect(sb_runtime_backup(runtime, bytes(*path), path->size(), &output) == SB_INVALID_ARGUMENT, "native C accepted an unrepresentable backup path");
        take(output);
    }
    // Backups are directories: the engine creates a new private one holding the copy and its manifest.
    Directory backups;
    const auto copy = (backups.path / "copy").string();
    expect(sb_runtime_backup(runtime, bytes(copy), copy.size(), &output) == SB_OK, "native C backup failed");
    expect(take(output).find("\"status\":\"ok\"") != std::string::npos, "native C backup report is not ok");
    expect(sb_verify_backup(bytes(copy), copy.size(), &output) == SB_OK, "native C could not verify its own backup");
    expect(take(output).find("\"verified\":true") != std::string::npos, "native C did not verify its own backup");
    expect(sb_runtime_backup(runtime, bytes(copy), copy.size(), &output) == SB_OK, "native C second backup call failed");
    expect(take(output).find("\"status\":\"error\"") != std::string::npos, "native C overwrote a nonempty backup directory");

    // A missing backup is a report that does not claim verification, never a success.
    const auto missing = (backups.path / "missing").string();
    const auto verified = sb_verify_backup(bytes(missing), missing.size(), &output);
    const auto refusal = take(output);
    expect(!refusal.empty() && refusal.find("\"verified\":true") == std::string::npos, "native C verified a missing backup");
    expect(verified != SB_OK || refusal.find("\"verified\":false") != std::string::npos, "native C missing backup report differs");
    expect(sb_verify_backup(nullptr, 3, &output) == SB_INVALID_ARGUMENT, "native C accepted an invalid backup pointer/count");
    take(output);
    expect(sb_verify_backup(bytes(missing), missing.size(), nullptr) == SB_INVALID_ARGUMENT, "native C verification accepted a missing output buffer");
    sb_runtime_destroy(runtime);
}

void cancellation()
{
    sb_runtime_cancel(nullptr);
    Directory directory;
    auto *runtime = create(switchboard::test::configuration(directory, {}));
    sb_runtime_cancel(runtime);
    expect(call_outcome(runtime, STATUS_REQUEST).outcome.status == SB_OUTCOME_OK, "native C idle cancellation affected a later call");

    // Cancellation may arrive from another thread while the owner calls; a core read has no web transfer to stop.
    std::thread canceller(
        [runtime]
        {
            for (int attempt = 0; attempt < 1000; ++attempt)
            {
                sb_runtime_cancel(runtime);
            }
        });
    const auto concurrent = call_outcome(runtime, STATUS_REQUEST);
    canceller.join();
    expect(concurrent.status == SB_OK && concurrent.outcome.status == SB_OUTCOME_OK, "native C concurrent cancellation broke a read");
    sb_runtime_destroy(runtime);
}
}  // namespace

int main()
{
    original_interface();
    revision_and_layout();
    core_outcomes();
    outcome_arguments();
    journaled_outcomes();
    operator_functions();
    cancellation();
    return 0;
}

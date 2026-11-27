// Shared cross-engine contract fixtures (contracts/cases) evaluated against the native engine.
//
// The fixtures are engine-neutral and contracts/cases/README.md documents their format. This runner drives the
// native Runtime through its public interface and checks the same expectations as the Python runner in
// tests/contracts/test_contracts.py. Engine-specific code is confined to NativeEngine. It prints every failed
// case and exits nonzero when any case fails or a known violation unexpectedly passes.

#include "support.hpp"

#include "../src/audit_internal.hpp"

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <fnmatch.h>

#ifndef SWITCHBOARD_CONTRACTS_DIR
#error "SWITCHBOARD_CONTRACTS_DIR must name the contracts/cases directory"
#endif

namespace
{
using switchboard::Json;

const char *const ENGINE = "native";
const char *const CASE_FORMAT = "switchboard-contract-cases";
const char *const TOOLS_FORMAT = "switchboard-contract-tools";
const char *const TOOLS_FILE = "synthetic-tools.json";
const char *const PROTOCOL_VERSION = "2026-07-28";
constexpr std::int64_t FORMAT_VERSION = 1;
constexpr std::size_t MAX_FIXTURE_BYTES = 8 * 1024 * 1024;
const std::set<std::string> STATUSES = {"ok", "pending", "denied", "invalid", "error"};
const std::set<std::string> STATES = {"in_flight", "succeeded", "failed", "uncertain", "reconciled_applied", "reconciled_not_applied"};
const std::set<std::string> ENVELOPE_KEYS = {"status", "tool", "action", "approval_id", "absent", "data", "instants", "patterns", "types", "message_contains"};
const std::set<std::string> LISTING_KEYS = {"listed", "not_listed", "listed_count", "all_listed_match", "none_listed_match"};
const std::set<std::string> RPC_KEYS = {"error_code", "is_error",     "structured_content", "outcome",          "listed",
                                        "not_listed", "listed_count", "all_listed_match",   "none_listed_match"};
const std::set<std::string> ENTRY_KEYS = {"entries", "count", "present_fields", "absent_fields"};

class Failure : public std::runtime_error
{

public:
    using std::runtime_error::runtime_error;
};

void check(const bool condition, const std::string &message)
{
    if (!condition)
    {
        throw Failure(message);
    }
}

std::string shown(const Json &value)
{
    auto text = value.dump(-1, ' ', true, Json::error_handler_t::replace);
    if (text.size() > 2000)
    {
        text.resize(2000);
        text += "...";
    }
    return text;
}

Json load(const std::filesystem::path &path)
{
    std::ifstream file(path, std::ios::binary);
    check(file.good(), "cannot read " + path.string());
    std::ostringstream text;
    text << file.rdbuf();
    // The engine's strict parser also refuses duplicate keys and non-finite numbers.
    return switchboard::parse_json(text.str(), MAX_FIXTURE_BYTES);
}

// Comparison

std::string json_type(const Json &value)
{
    if (value.is_null())
    {
        return "null";
    }
    if (value.is_boolean())
    {
        return "boolean";
    }
    if (value.is_number_integer())
    {
        return "integer";
    }
    if (value.is_number_float())
    {
        return "number";
    }
    if (value.is_string())
    {
        return "string";
    }
    if (value.is_array())
    {
        return "array";
    }
    return value.is_object() ? "object" : "other";
}

bool has_type(const Json &value, const std::string &name)
{
    const auto actual = json_type(value);
    return actual == name || (name == "number" && actual == "integer");
}

// JSON scalar equality: booleans never equal numbers, while 1 equals 1.0.
bool same_value(const Json &expected, const Json &actual)
{
    if (expected.is_boolean() || actual.is_boolean())
    {
        return expected.is_boolean() && actual.is_boolean() && expected == actual;
    }
    if (expected.is_number() && actual.is_number())
    {
        return expected == actual;
    }
    return json_type(expected) == json_type(actual) && expected == actual;
}

// Listed object keys must match; arrays must have the same length and match item by item.
bool subset(const Json &expected, const Json &actual)
{
    if (expected.is_object())
    {
        if (!actual.is_object())
        {
            return false;
        }
        for (auto item = expected.begin(); item != expected.end(); ++item)
        {
            if (!actual.contains(item.key()) || !subset(item.value(), actual.at(item.key())))
            {
                return false;
            }
        }
        return true;
    }
    if (expected.is_array())
    {
        if (!actual.is_array() || actual.size() != expected.size())
        {
            return false;
        }
        for (std::size_t index = 0; index < expected.size(); ++index)
        {
            if (!subset(expected.at(index), actual.at(index)))
            {
                return false;
            }
        }
        return true;
    }
    return same_value(expected, actual);
}

bool match_from(const Json &expected, const std::size_t index, const Json &actual, std::vector<bool> &used)
{
    if (index == expected.size())
    {
        return true;
    }
    for (std::size_t candidate = 0; candidate < actual.size(); ++candidate)
    {
        if (!used[candidate] && subset(expected.at(index), actual.at(candidate)))
        {
            used[candidate] = true;
            if (match_from(expected, index + 1, actual, used))
            {
                return true;
            }
            used[candidate] = false;
        }
    }
    return false;
}

// One-to-one subset match in any order.
bool match_unordered(const Json &expected, const Json &actual)
{
    if (!expected.is_array() || !actual.is_array() || expected.size() != actual.size())
    {
        return false;
    }
    std::vector<bool> used(actual.size(), false);
    return match_from(expected, 0, actual, used);
}

std::optional<Json> lookup(const Json &value, const std::string &path)
{
    const Json *current = &value;
    std::size_t start = 0;
    while (start <= path.size())
    {
        const auto end = std::min(path.find('.', start), path.size());
        const auto part = path.substr(start, end - start);
        const bool index = !part.empty() && part.size() < 10 && part.find_first_not_of("0123456789") == std::string::npos;
        if (current->is_object() && current->contains(part))
        {
            current = &current->at(part);
        }
        else if (current->is_array() && index && std::stoul(part) < current->size())
        {
            current = &current->at(std::stoul(part));
        }
        else
        {
            return std::nullopt;
        }
        start = end + 1;
    }
    return std::optional<Json>(std::in_place, *current);
}

std::int64_t instant(const Json &value)
{
    check(value.is_string(), "expected a timestamp, got " + shown(value));
    return switchboard::parse_timestamp(value.get<std::string>());
}

bool glob(const std::string &pattern, const std::string &name)
{
    return fnmatch(pattern.c_str(), name.c_str(), 0) == 0;
}

std::string joined(const std::vector<std::string> &names)
{
    std::string result;
    for (const auto &name : names)
    {
        result += (result.empty() ? "" : ", ") + name;
    }
    return "[" + result + "]";
}

void only(const Json &expected, const std::set<std::string> &allowed)
{
    check(expected.is_object(), "expectations must be an object");
    for (auto item = expected.begin(); item != expected.end(); ++item)
    {
        check(allowed.contains(item.key()), "unknown expectation " + item.key());
    }
}

void check_paths(const Json &value, const Json &expected)
{
    const auto text = shown(value);
    const auto instants = expected.value("instants", Json::object());
    for (auto item = instants.begin(); item != instants.end(); ++item)
    {
        const auto have = lookup(value, item.key());
        check(have.has_value(), item.key() + " is missing in " + text);
        if (item.value().is_null())
        {
            check(have->is_null(), "expected " + item.key() + " to be null in " + text);
        }
        else
        {
            check(!have->is_null() && instant(*have) == instant(item.value()), "expected " + item.key() + " at " + item.value().dump() + " in " + text);
        }
    }
    const auto patterns = expected.value("patterns", Json::object());
    for (auto item = patterns.begin(); item != patterns.end(); ++item)
    {
        const auto have = lookup(value, item.key());
        check(have && have->is_string() && std::regex_match(have->get<std::string>(), std::regex(item.value().get<std::string>())),
              "expected " + item.key() + " to match " + item.value().dump() + " in " + text);
    }
    const auto types = expected.value("types", Json::object());
    for (auto item = types.begin(); item != types.end(); ++item)
    {
        const auto have = lookup(value, item.key());
        check(have && has_type(*have, item.value().get<std::string>()), "expected " + item.key() + " to be " + item.value().dump() + " in " + text);
    }
}

// The contract's outcome envelope, plus the step's expectations.
void check_envelope(const Json &envelope, const Json &expected)
{
    static const std::regex action_id("[0-9a-f]{32}");
    only(expected, ENVELOPE_KEYS);
    check(envelope.is_object(), "expected an outcome envelope, got " + shown(envelope));
    const auto text = shown(envelope);
    check(envelope.contains("status") && envelope.at("status").is_string() && STATUSES.contains(envelope.at("status").get<std::string>()) &&
              envelope.contains("tool") && envelope.at("tool").is_string(),
          "malformed envelope " + text);
    if (envelope.contains("action"))
    {
        const auto &action = envelope.at("action");
        check(action.is_object() && action.size() == 2 && action.contains("id") && action.at("id").is_string() &&
                  std::regex_match(action.at("id").get<std::string>(), action_id) && action.contains("state") && action.at("state").is_string() &&
                  STATES.contains(action.at("state").get<std::string>()),
              "malformed action in " + text);
    }
    for (const auto *key : {"status", "tool"})
    {
        if (expected.contains(key))
        {
            check(envelope.at(key) == expected.at(key), std::string("expected ") + key + " " + expected.at(key).dump() + " in " + text);
        }
    }
    if (expected.contains("action"))
    {
        const auto &want = expected.at("action");
        if (want.is_null())
        {
            check(!envelope.contains("action"), "expected no action in " + text);
        }
        else
        {
            check(envelope.contains("action") && envelope.at("action").at("state") == want, "expected action state " + want.dump() + " in " + text);
        }
    }
    if (expected.contains("approval_id"))
    {
        check(envelope.contains("approval_id") == expected.at("approval_id").get<bool>(), "approval_id presence differs in " + text);
    }
    const auto absent = expected.value("absent", Json::array());
    for (const auto &key : absent)
    {
        check(!envelope.contains(key.get<std::string>()), "expected no " + key.get<std::string>() + " in " + text);
    }
    if (expected.contains("data"))
    {
        check(envelope.contains("data") && subset(expected.at("data"), envelope.at("data")), "expected data " + shown(expected.at("data")) + " in " + text);
    }
    check_paths(envelope, expected);
    const auto message = envelope.value("message", std::string());
    const auto parts = expected.value("message_contains", Json::array());
    for (const auto &part : parts)
    {
        check(part.is_string() && message.find(part.get<std::string>()) != std::string::npos, "expected the message to contain " + part.dump() + " in " + text);
    }
}

void check_listing(const Json &result, const Json &expected, const std::string &text)
{
    check(result.contains("tools") && result.at("tools").is_array(), "expected a tool listing in " + text);
    std::vector<std::string> names;
    for (const auto &tool : result.at("tools"))
    {
        names.push_back(tool.at("name").get<std::string>());
    }
    const auto listed = [&](const Json &name)
    {
        return std::find(names.begin(), names.end(), name.get<std::string>()) != names.end();
    };
    const auto wanted = expected.value("listed", Json::array());
    for (const auto &name : wanted)
    {
        check(listed(name), "expected " + name.dump() + " to be listed in " + joined(names));
    }
    const auto unwanted = expected.value("not_listed", Json::array());
    for (const auto &name : unwanted)
    {
        check(!listed(name), "expected " + name.dump() + " not to be listed in " + joined(names));
    }
    if (expected.contains("listed_count"))
    {
        check(names.size() == expected.at("listed_count").get<std::size_t>(), "expected " + expected.at("listed_count").dump() + " tools in " + joined(names));
    }
    const auto all = expected.value("all_listed_match", Json::array());
    const auto none = expected.value("none_listed_match", Json::array());
    for (const auto &name : names)
    {
        if (expected.contains("all_listed_match"))
        {
            check(std::any_of(all.begin(), all.end(),
                              [&](const Json &pattern)
                              {
                                  return glob(pattern.get<std::string>(), name);
                              }),
                  name + " matches none of " + all.dump());
        }
        for (const auto &pattern : none)
        {
            check(!glob(pattern.get<std::string>(), name), name + " must not match " + pattern.dump());
        }
    }
}

void check_rpc(const Json &reply, const Json &expected)
{
    only(expected, RPC_KEYS);
    const auto text = shown(reply);
    check(reply.is_object() && reply.value("jsonrpc", Json()) == "2.0", "malformed protocol reply " + text);
    if (expected.contains("error_code"))
    {
        const auto code = lookup(reply, "error.code");
        check(!reply.contains("result") && code && same_value(expected.at("error_code"), *code),
              "expected protocol error " + expected.at("error_code").dump() + " in " + text);
        return;
    }
    check(!reply.contains("error") && reply.contains("result") && reply.at("result").is_object(), "expected a protocol result in " + text);
    const auto &result = reply.at("result");
    if (expected.contains("is_error"))
    {
        check(result.contains("isError") && result.at("isError").is_boolean() && result.at("isError") == expected.at("is_error"),
              "expected isError " + expected.at("is_error").dump() + " in " + text);
    }
    if (expected.contains("structured_content"))
    {
        check(result.contains("structuredContent") == expected.at("structured_content").get<bool>(), "structuredContent presence differs in " + text);
    }
    if (expected.contains("outcome"))
    {
        const auto content = lookup(result, "content.0.text");
        check(content && content->is_string(), "expected text content in " + text);
        check_envelope(switchboard::parse_json(content->get<std::string>()), expected.at("outcome"));
    }
    if (std::any_of(LISTING_KEYS.begin(), LISTING_KEYS.end(),
                    [&](const std::string &key)
                    {
                        return expected.contains(key);
                    }))
    {
        check_listing(result, expected, text);
    }
}

void check_entries(const Json &entries, const Json &expected)
{
    only(expected, ENTRY_KEYS);
    const auto text = shown(entries);
    if (expected.contains("entries"))
    {
        check(match_unordered(expected.at("entries"), entries), "expected entries " + shown(expected.at("entries")) + " in " + text);
    }
    if (expected.contains("count"))
    {
        check(entries.size() == expected.at("count").get<std::size_t>(), "expected " + expected.at("count").dump() + " entries in " + text);
    }
    const auto present = expected.value("present_fields", Json::array());
    const auto absent = expected.value("absent_fields", Json::array());
    for (const auto &entry : entries)
    {
        check(entry.is_object(), "malformed entry in " + text);
        for (const auto &field : present)
        {
            check(entry.contains(field.get<std::string>()), "expected " + field.dump() + " in every entry of " + text);
        }
        for (const auto &field : absent)
        {
            check(!entry.contains(field.get<std::string>()), "expected no " + field.dump() + " in any entry of " + text);
        }
    }
}

bool schema_accepted(const Json &schema)
{
    try
    {
        switchboard::check_schema(schema);
    }
    catch (const switchboard::Error &)
    {
        return false;
    }
    return true;
}

bool value_accepted(const Json &value, const Json &schema)
{
    try
    {
        switchboard::validate(value, schema);
    }
    catch (const switchboard::Error &)
    {
        return false;
    }
    return true;
}

bool registration_accepted(const Json &item)
{
    switchboard::Registry registry;
    switchboard::Tool tool;
    tool.name = item.at("name").get<std::string>();
    tool.description = "Contract probe.";
    tool.tier = switchboard::Tier::read;
    tool.schema = item.at("schema");
    tool.handler = [](const Json &)
    {
        return Json::object();
    };
    try
    {
        registry.add(std::move(tool));
    }
    catch (const switchboard::Error &)
    {
        return false;
    }
    return true;
}

switchboard::Origin origin_of(const std::string &name)
{
    if (name == "assistant")
    {
        return switchboard::Origin::assistant;
    }
    if (name == "rule")
    {
        return switchboard::Origin::rule;
    }
    check(name == "human", "unknown origin " + name);
    return switchboard::Origin::human;
}

switchboard::Tier tier_of(const std::string &name)
{
    if (name == "read")
    {
        return switchboard::Tier::read;
    }
    if (name == "draft")
    {
        return switchboard::Tier::draft;
    }
    if (name == "act")
    {
        return switchboard::Tier::act;
    }
    check(name == "spend", "unknown tier " + name);
    return switchboard::Tier::spend;
}

// The native engine behind the neutral step model.
class NativeEngine
{

public:
    explicit NativeEngine(const Json &tools) : tools_(tools)
    {
    }

    NativeEngine(const NativeEngine &) = delete;
    NativeEngine &operator=(const NativeEngine &) = delete;

    // Synthetic handler runs per tool name; kept across restarts.
    std::map<std::string, std::int64_t> calls;

    static Json constant(const std::string &name)
    {
        check(name == "absent_approval_id", "unknown engine constant " + name);
        return std::string(32, '0');
    }

    // Starts the runtime on the case's private directory (reopening it on restart) with the synthetic tools.
    void start(const Json &config)
    {
        if (!directory_)
        {
            directory_.emplace();
        }
        runtime_.reset();
        auto configuration = config;
        configuration["switchboard"]["data_dir"] = directory_->path.string();
        runtime_ = std::make_unique<switchboard::Runtime>(configuration, std::make_unique<switchboard::test::FakeHttp>(), switchboard::Clock{}, extension());
    }

    Json call(const std::string &tool, const Json &arguments, const std::string &origin)
    {
        return runtime().call(tool, arguments, origin_of(origin));
    }

    Json approve(const std::string &id)
    {
        return runtime().approve(id);
    }

    bool deny(const std::string &id)
    {
        const auto result = runtime().deny(id);
        return result.is_object() && result.value("status", std::string()) == "denied";
    }

    // The state after an accepted decision, or nothing when the engine refused it.
    std::optional<std::string> reconcile(const std::string &id, const std::string &resolution, const std::string &note)
    {
        check(resolution == "applied" || resolution == "not_applied", "unknown resolution " + resolution);
        const auto report = runtime().reconcile(id, resolution, note);
        if (report.value("status", std::string()) != "ok")
        {
            return std::nullopt;
        }
        return report.at("action").at("state").get<std::string>();
    }

    std::optional<std::string> action_state(const std::string &id)
    {
        const auto report = runtime().actions(Json{{"id", id}});
        if (report.value("status", std::string()) != "ok")
        {
            return std::nullopt;
        }
        return report.at("action").at("state").get<std::string>();
    }

    std::pair<Json, Json> recent_actions(const Json &arguments)
    {
        auto outcome = call("actions_recent", arguments, "assistant");
        Json entries = nullptr;
        if (outcome.contains("data"))
        {
            const auto &data = outcome.at("data");
            entries = data.is_object() ? data.value("actions", Json()) : data;
        }
        return {outcome, entries};
    }

    void kill_switch(const bool on)
    {
        const auto report = runtime().kill_switch(on);
        check(report.value("status", std::string()) == "ok", "kill switch: " + shown(report));
    }

    void tick()
    {
        (void)runtime().tick();
    }

    // An in-flight entry as a process that ended mid-call leaves it, written directly to native_actions.
    std::string insert_stale_action(const std::string &tool, const Json &arguments, const std::int64_t started, const std::int64_t deadline)
    {
        (void)runtime();
        std::ostringstream id;
        id << std::hex << std::setw(32) << std::setfill('0') << ++inserted_;
        const std::int64_t now = std::chrono::duration_cast<std::chrono::seconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        switchboard::Database editor(directory_->path / "native-state.sqlite3");
        editor.execute("INSERT INTO native_actions(id, tool, args_digest, args, origin, approval_id, state, owner, started_at, deadline_at) "
                       "VALUES (?, ?, ?, ?, 'assistant', NULL, 'in_flight', 'contract:ended-process', ?, ?)",
                       {id.str(), tool, switchboard::hash(arguments), arguments.dump(-1, ' ', true), now + started, now + deadline});
        return id.str();
    }

    Json rpc(const Json &message)
    {
        return runtime().rpc(message);
    }

private:
    switchboard::Runtime &runtime()
    {
        check(runtime_ != nullptr, "the engine has not started");
        return *runtime_;
    }

    switchboard::Extension extension()
    {
        return [this](switchboard::Context &, switchboard::Registry &registry)
        {
            for (const auto &spec : tools_.at("tools"))
            {
                switchboard::Tool tool;
                tool.name = spec.at("name").get<std::string>();
                tool.description = "Contract synthetic " + spec.at("tier").get<std::string>() + " tool.";
                tool.tier = tier_of(spec.at("tier").get<std::string>());
                tool.schema = tools_.at("schema");
                tool.handler = [this, name = tool.name](const Json &arguments)
                {
                    return handle(name, arguments);
                };
                registry.add(std::move(tool));
            }
        };
    }

    Json handle(const std::string &name, const Json &arguments)
    {
        ++calls[name];
        const auto &mode = tools_.at("modes").at(arguments.at("mode").get<std::string>());
        if (mode.contains("raises"))
        {
            const auto kind = mode.at("raises").get<std::string>();
            if (kind == "failed")
            {
                throw switchboard::Error("contract_rejected", "the contract provider refused the change before applying it");
            }
            if (kind == "uncertain")
            {
                throw switchboard::Error("uncertain", "the contract change may have been applied");
            }
            throw std::runtime_error("contract handler failed unexpectedly");
        }
        return mode.at("returns");
    }

    Json tools_;
    std::optional<switchboard::test::Directory> directory_;
    std::unique_ptr<switchboard::Runtime> runtime_;
    std::uint64_t inserted_ = 0;
};

// The step loop, shared in shape with tests/contracts/test_contracts.py.
class CaseRunner
{

public:
    CaseRunner(const Json &item, const Json &tools) : case_(item), engine_(tools)
    {
    }

    void run()
    {
        std::size_t number = 0;
        for (const auto &step : case_.at("steps"))
        {
            ++number;
            try
            {
                perform(step);
            }
            catch (const std::exception &error)
            {
                throw Failure("step " + std::to_string(number) + " (" + step.value("op", std::string("?")) + "): " + error.what());
            }
        }
    }

private:
    Json expectation(const Json &step) const
    {
        auto expected = step.value("expect", Json::object());
        if (step.contains("divergence"))
        {
            const auto &mine = step.at("divergence").at(ENGINE);
            for (auto item = mine.begin(); item != mine.end(); ++item)
            {
                expected[item.key()] = item.value();
            }
        }
        return expected;
    }

    Json resolve(const Json &value) const
    {
        if (value.is_object())
        {
            if (value.size() == 1 && value.contains("$saved"))
            {
                const auto name = value.at("$saved").get<std::string>();
                const auto found = saved_.find(name);
                check(found != saved_.end(), "nothing was saved as " + name);
                return found->second;
            }
            if (value.size() == 1 && value.contains("$engine"))
            {
                return NativeEngine::constant(value.at("$engine").get<std::string>());
            }
            Json result = Json::object();
            for (auto item = value.begin(); item != value.end(); ++item)
            {
                result[item.key()] = resolve(item.value());
            }
            return result;
        }
        if (value.is_array())
        {
            Json result = Json::array();
            for (const auto &item : value)
            {
                result.push_back(resolve(item));
            }
            return result;
        }
        return value;
    }

    void save(const Json &step, const Json &result)
    {
        const auto names = step.value("save", Json::object());
        for (auto item = names.begin(); item != names.end(); ++item)
        {
            const auto path = item.value().get<std::string>();
            const auto found = lookup(result, path);
            check(found.has_value(), "cannot save " + path + " from " + shown(result));
            saved_[item.key()] = *found;
        }
    }

    void perform(const Json &step)
    {
        const auto op = step.at("op").get<std::string>();
        auto expected = expectation(step);
        if (op == "check_schema")
        {
            only(expected, {"valid", "invalid"});
            const auto valid = expected.value("valid", Json::array());
            const auto invalid = expected.value("invalid", Json::array());
            for (const auto &schema : valid)
            {
                check(schema_accepted(schema), "definition refused: " + shown(schema));
            }
            for (const auto &schema : invalid)
            {
                check(!schema_accepted(schema), "definition accepted: " + shown(schema));
            }
            return;
        }
        if (op == "validate")
        {
            only(expected, {"valid", "invalid"});
            const auto &schema = step.at("schema");
            check(schema_accepted(schema), "validate needs an accepted schema: " + shown(schema));
            const auto valid = expected.value("valid", Json::array());
            const auto invalid = expected.value("invalid", Json::array());
            for (const auto &value : valid)
            {
                check(value_accepted(value, schema), "value refused: " + shown(value));
            }
            for (const auto &value : invalid)
            {
                check(!value_accepted(value, schema), "value accepted: " + shown(value));
            }
            return;
        }
        if (op == "register")
        {
            only(expected, {"accepted", "refused"});
            const auto accepted = expected.value("accepted", Json::array());
            const auto refused = expected.value("refused", Json::array());
            for (const auto &item : accepted)
            {
                check(registration_accepted(item), "registration refused: " + shown(item));
            }
            for (const auto &item : refused)
            {
                check(!registration_accepted(item), "registration accepted: " + shown(item));
            }
            return;
        }
        if (op == "redact")
        {
            only(expected, {"result"});
            check(expected.contains("result"), "redact needs an expected result");
            std::vector<std::string> names;
            for (const auto &name : step.value("hash_only", Json::array()))
            {
                names.push_back(name.get<std::string>());
            }
            const auto result = switchboard::audit::redact(step.at("value"), names);
            const auto &want = expected.at("result");
            check(subset(want, result) && subset(result, want), "expected " + shown(want) + ", got " + shown(result));
            return;
        }
        expected = resolve(expected);
        if (op == "restart")
        {
            engine_.start(step.contains("config") ? step.at("config") : case_.value("config", Json::object()));
            started_ = true;
            return;
        }
        if (!started_)
        {
            engine_.start(case_.value("config", Json::object()));
            started_ = true;
        }
        perform_on_engine(op, step, expected);
    }

    void perform_on_engine(const std::string &op, const Json &step, const Json &expected)
    {
        if (op == "call")
        {
            const auto result =
                engine_.call(step.at("tool").get<std::string>(), resolve(step.value("args", Json::object())), step.value("origin", std::string("assistant")));
            check_envelope(result, expected);
            save(step, result);
        }
        else if (op == "approve")
        {
            const auto result = engine_.approve(resolve(step.at("approval")).get<std::string>());
            check_envelope(result, expected);
            save(step, result);
        }
        else if (op == "deny")
        {
            only(expected, {"denied"});
            const bool denied = engine_.deny(resolve(step.at("approval")).get<std::string>());
            check(denied == expected.at("denied").get<bool>(), "expected denied " + expected.at("denied").dump());
        }
        else if (op == "reconcile")
        {
            only(expected, {"accepted", "state"});
            const auto state =
                engine_.reconcile(resolve(step.at("action")).get<std::string>(), step.at("resolution").get<std::string>(), step.at("note").get<std::string>());
            check(state.has_value() == expected.at("accepted").get<bool>(), "expected accepted " + expected.at("accepted").dump());
            if (expected.contains("state"))
            {
                check(state && *state == expected.at("state").get<std::string>(),
                      "expected state " + expected.at("state").dump() + ", got " + state.value_or("none"));
            }
        }
        else if (op == "action_state")
        {
            only(expected, {"state"});
            const auto state = engine_.action_state(resolve(step.at("action")).get<std::string>());
            const auto &want = expected.at("state");
            check(want.is_null() ? !state.has_value() : state && *state == want.get<std::string>(),
                  "expected state " + want.dump() + ", got " + state.value_or("none"));
        }
        else if (op == "recent_actions")
        {
            const auto [outcome, entries] = engine_.recent_actions(resolve(step.value("args", Json::object())));
            check(outcome.value("status", std::string()) == "ok" && entries.is_array(), "actions_recent: " + shown(outcome));
            check_entries(entries, expected);
        }
        else if (op == "handler_calls")
        {
            for (auto item = expected.begin(); item != expected.end(); ++item)
            {
                const auto found = engine_.calls.find(item.key());
                const std::int64_t runs = found == engine_.calls.end() ? 0 : found->second;
                check(runs == item.value().get<std::int64_t>(), "expected " + item.value().dump() + " " + item.key() + " runs, got " + std::to_string(runs));
            }
        }
        else if (op == "kill_switch")
        {
            engine_.kill_switch(step.at("on").get<bool>());
        }
        else if (op == "tick")
        {
            engine_.tick();
        }
        else if (op == "insert_stale_action")
        {
            saved_[step.at("save").get<std::string>()] =
                engine_.insert_stale_action(step.at("tool").get<std::string>(), resolve(step.at("args")), step.at("started_offset_seconds").get<std::int64_t>(),
                                            step.at("deadline_offset_seconds").get<std::int64_t>());
        }
        else if (op == "rpc")
        {
            auto params = resolve(step.value("params", Json::object()));
            params["_meta"] = Json{{"io.modelcontextprotocol/protocolVersion", PROTOCOL_VERSION}};
            const auto reply = engine_.rpc(Json{{"jsonrpc", "2.0"}, {"id", ++request_id_}, {"method", step.at("method")}, {"params", params}});
            check_rpc(reply, expected);
            save(step, reply);
        }
        else
        {
            throw Failure("unknown operation " + op);
        }
    }

    const Json &case_;
    NativeEngine engine_;
    std::map<std::string, Json> saved_;
    bool started_ = false;
    std::int64_t request_id_ = 0;
};

void check_document(const Json &document)
{
    check(document.is_object() && document.value("format", std::string()) == CASE_FORMAT, std::string("not a ") + CASE_FORMAT + " file");
    check(document.value("version", Json()) == FORMAT_VERSION, "unsupported fixture format version " + document.value("version", Json()).dump());
    check(document.contains("cases") && document.at("cases").is_array() && !document.at("cases").empty(), "expected a non-empty list of cases");
    std::set<std::string> ids;
    for (const auto &item : document.at("cases"))
    {
        check(item.is_object() && item.contains("id") && item.at("id").is_string() && ids.insert(item.at("id").get<std::string>()).second,
              "every case needs a unique id");
        check(item.contains("steps") && item.at("steps").is_array() && !item.at("steps").empty(), "case " + item.at("id").get<std::string>() + " has no steps");
    }
}

std::optional<std::string> known_violation(const Json &item)
{
    if (!item.contains("known_violation") || !item.at("known_violation").contains(ENGINE))
    {
        return std::nullopt;
    }
    return item.at("known_violation").at(ENGINE).get<std::string>();
}
}  // namespace

int main()
{
    const std::filesystem::path root = SWITCHBOARD_CONTRACTS_DIR;
    Json tools;
    std::vector<std::filesystem::path> files;
    try
    {
        tools = load(root / TOOLS_FILE);
        check(tools.value("format", std::string()) == TOOLS_FORMAT && tools.value("version", Json()) == FORMAT_VERSION, "unsupported synthetic tools file");
        for (const auto &entry : std::filesystem::directory_iterator(root))
        {
            if (entry.path().extension() == ".json" && entry.path().filename() != TOOLS_FILE)
            {
                files.push_back(entry.path());
            }
        }
        std::sort(files.begin(), files.end());
        check(!files.empty(), "no case files in " + root.string());
    }
    catch (const std::exception &error)
    {
        std::cerr << "FAIL contracts: " << error.what() << "\n";
        return 1;
    }
    std::vector<std::string> failures;
    std::size_t passed = 0;
    std::size_t expected_failures = 0;
    for (const auto &path : files)
    {
        Json document;
        try
        {
            document = load(path);
            check_document(document);
        }
        catch (const std::exception &error)
        {
            failures.push_back(path.filename().string() + ": " + error.what());
            continue;
        }
        for (const auto &item : document.at("cases"))
        {
            const auto label = path.stem().string() + "/" + item.at("id").get<std::string>();
            const auto violation = known_violation(item);
            std::string problem;
            try
            {
                CaseRunner(item, tools).run();
            }
            catch (const std::exception &error)
            {
                problem = std::string("failed: ") + error.what();
            }
            if (violation && problem.empty())
            {
                failures.push_back(label + ": passed although it is marked as a known native violation; remove the marker (" + *violation + ")");
            }
            else if (violation)
            {
                ++expected_failures;
                std::cout << "XFAIL " << label << ": " << *violation << "\n";
            }
            else if (problem.empty())
            {
                ++passed;
            }
            else
            {
                failures.push_back(label + ": " + problem);
            }
        }
    }
    for (const auto &failure : failures)
    {
        std::cerr << "FAIL " << failure << "\n";
    }
    std::cout << "contracts: " << passed << " passed, " << expected_failures << " expected failures, " << failures.size() << " failed\n";
    return failures.empty() ? 0 : 1;
}

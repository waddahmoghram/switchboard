// libFuzzer target: schema definitions and validation pairs.
//
// Input: one selector byte, then text. Even selectors split the text at the first newline into a fuzzed
// schema and a fuzzed value. The schema goes through check_schema with its patterns; validation then runs
// against a copy without patterns, because schemas are trusted configuration and the standard library's
// backtracking regular expressions are not a bounded-time engine for adversarial patterns. Odd selectors
// validate a fuzzed value against fixed, bounded engine-style schemas that do carry patterns.
#include "common.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

using switchboard::Error;
using switchboard::Json;
using switchboard::fuzz::check;
using switchboard::fuzz::fail;

namespace
{
void strip_patterns(Json &schema)
{
    schema.erase("pattern");
    if (schema.contains("properties"))
    {
        for (auto &child : schema["properties"])
        {
            strip_patterns(child);
        }
    }
    for (const auto *key : {"items", "propertyNames"})
    {
        if (schema.contains(key))
        {
            strip_patterns(schema[key]);
        }
    }
}

// Returns whether the value validated; any rejection other than "invalid" breaks the validator contract.
bool validates(const Json &value, const Json &schema)
{
    try
    {
        switchboard::validate(value, schema);
        return true;
    }
    catch (const Error &error)
    {
        check(error.code() == "invalid", "a checked schema produced a non-validation error");
        return false;
    }
}

void check_pair(const Json &value, const Json &schema)
{
    const bool accepted = validates(value, schema);
    check(validates(value, schema) == accepted, "validation is not deterministic");
    if (accepted)
    {
        const auto again = switchboard::fuzz::parsed(value.dump());
        check(again.has_value() && validates(*again, schema), "a valid value stopped validating after a round trip");
    }
}

Json engine_schema(const std::uint8_t selector)
{
    const std::array<Json, 4> schemas = {
        switchboard::object_schema({{"id", {{"type", "string"}, {"pattern", "^[0-9a-f]{32}$"}, {"maxLength", 32}}}}, {"id"}),
        switchboard::object_schema({{"name", {{"type", "string"}, {"pattern", "^[a-z][a-z0-9_]{0,63}$"}, {"maxLength", 64}}},
                                    {"limit", {{"type", "integer"}, {"minimum", 1}, {"maximum", 100}}}},
                                   {"name"}),
        switchboard::object_schema({{"day", {{"type", "string"}, {"pattern", "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}, {"maxLength", 10}}},
                                    {"tags", {{"type", "array"}, {"maxItems", 8}, {"items", {{"type", "string"}, {"maxLength", 32}}}}}}),
        switchboard::object_schema({{"mode", {{"type", "string"}, {"enum", Json::array({"on", "off"})}}},
                                    {"level", {{"type", "number"}, {"minimum", 0}, {"maximum", 1}}},
                                    {"note", {{"type", {"string", "null"}}, {"maxLength", 500}}}},
                                   {"mode"})};
    return schemas[selector % schemas.size()];
}
}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    if (size < 2)
    {
        return 0;
    }
    const auto selector = data[0];
    const auto input = switchboard::fuzz::text(data + 1, size - 1);
    if ((selector & 1U) != 0)
    {
        const auto schema = engine_schema(data[1]);
        try
        {
            switchboard::check_schema(schema);
        }
        catch (const Error &)
        {
            fail("a fixed engine-style schema was rejected");
        }
        if (const auto value = switchboard::fuzz::parsed(input.substr(1)))
        {
            check_pair(*value, schema);
        }
        return 0;
    }
    const auto newline = input.find('\n');
    if (newline == std::string::npos)
    {
        return 0;
    }
    auto schema = switchboard::fuzz::parsed(input.substr(0, newline));
    const auto value = switchboard::fuzz::parsed(input.substr(newline + 1));
    if (!schema)
    {
        return 0;
    }
    try
    {
        switchboard::check_schema(*schema);
    }
    catch (const Error &error)
    {
        check(error.code() == "schema" || error.code() == "invalid", "schema checking failed with an unexpected error code");
        return 0;
    }
    strip_patterns(*schema);
    try
    {
        switchboard::check_schema(*schema);
    }
    catch (const Error &)
    {
        fail("removing patterns made a checked schema invalid");
    }
    if (value)
    {
        check_pair(*value, *schema);
    }
    return 0;
}

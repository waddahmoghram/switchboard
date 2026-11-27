// libFuzzer target: the bounded JSON parser, empty-schema validation and canonical hashing.
// Invariants: accepted JSON validates, hashes to 64 lowercase hexadecimal digits,
// and survives a dump/parse round trip with an identical value and hash.
#include "common.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>

using switchboard::Error;
using switchboard::Json;
using switchboard::fuzz::check;
using switchboard::fuzz::fail;

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    const auto value = switchboard::fuzz::parsed(switchboard::fuzz::text(data, size));
    if (!value)
    {
        return 0;
    }
    std::string digest;
    try
    {
        switchboard::validate(*value, Json::object());
        digest = switchboard::hash(*value);
    }
    catch (const Error &)
    {
        fail("parsed JSON was rejected by empty-schema validation or hashing");
    }
    check(digest.size() == 64 && std::all_of(digest.begin(), digest.end(),
                                             [](const char character)
                                             {
                                                 return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
                                             }),
          "hash is not 64 lowercase hexadecimal digits");
    const auto again = switchboard::fuzz::parsed(value->dump());
    check(again.has_value(), "the engine rejected its own serialized JSON");
    check(*again == *value, "dump and parse did not round-trip the value");
    try
    {
        check(switchboard::hash(*again) == digest, "equal values hashed differently");
    }
    catch (const Error &)
    {
        fail("a round-tripped value could not be hashed");
    }
    return 0;
}

#pragma once

#include "switchboard/native.hpp"

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <optional>
#include <string>

// Shared helpers for the libFuzzer targets. Authored and unrun: no target has been built or fuzzed yet.
namespace switchboard::fuzz
{
// Reports a violated invariant and aborts, so libFuzzer records the current input as a crash.
[[noreturn]] inline void fail(const char *invariant) noexcept
{
    std::fprintf(stderr, "INVARIANT VIOLATED: %s\n", invariant);
    std::abort();
}

inline void check(const bool condition, const char *invariant) noexcept
{
    if (!condition)
    {
        fail(invariant);
    }
}

inline std::string text(const std::uint8_t *data, const std::size_t size)
{
    return size == 0 ? std::string() : std::string(reinterpret_cast<const char *>(data), size);
}

// Parses with the engine's own bounded parser; malformed input is an expected rejection.
inline std::optional<Json> parsed(const std::string &input)
{
    try
    {
        return parse_json(input);
    }
    catch (const Error &)
    {
        return std::nullopt;
    }
}

inline std::string lowercase(std::string value)
{
    for (auto &character : value)
    {
        if (character >= 'A' && character <= 'Z')
        {
            character = static_cast<char>(character - 'A' + 'a');
        }
    }
    return value;
}
}  // namespace switchboard::fuzz

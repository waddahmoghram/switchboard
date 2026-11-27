// libFuzzer target: timestamp parsing and rendering.
// Invariants: every accepted text is an instant from 1970 through 9999 that renders as YYYY-MM-DDTHH:MM:SSZ
// and parses back to the same instant; rendering rejects exactly the instants outside that range.
#include "common.hpp"

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <optional>
#include <string>

using switchboard::Error;
using switchboard::fuzz::check;
using switchboard::fuzz::fail;

namespace
{
constexpr std::int64_t LAST_INSTANT = 253402300799LL;  // 9999-12-31T23:59:59Z

std::optional<std::int64_t> parse(const std::string &text)
{
    try
    {
        return switchboard::parse_timestamp(text);
    }
    catch (const Error &)
    {
        return std::nullopt;
    }
}

void check_rendering(const std::int64_t seconds)
{
    std::string rendered;
    try
    {
        rendered = switchboard::timestamp(seconds);
    }
    catch (const Error &)
    {
        fail("an in-range instant could not be rendered");
    }
    check(rendered.size() == 20 && rendered[4] == '-' && rendered[7] == '-' && rendered[10] == 'T' && rendered[13] == ':' && rendered[16] == ':' &&
              rendered[19] == 'Z',
          "rendered timestamp does not have the YYYY-MM-DDTHH:MM:SSZ shape");
    const auto again = parse(rendered);
    check(again.has_value() && *again == seconds, "a rendered timestamp did not parse back to the same instant");
}
}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    if (const auto seconds = parse(switchboard::fuzz::text(data, size)))
    {
        check(*seconds >= 0 && *seconds <= LAST_INSTANT, "parsed instant is outside the supported range");
        check_rendering(*seconds);
    }
    if (size >= sizeof(std::int64_t))
    {
        std::int64_t seconds = 0;
        std::memcpy(&seconds, data, sizeof(seconds));
        bool rendered = true;
        try
        {
            (void)switchboard::timestamp(seconds);
        }
        catch (const Error &)
        {
            rendered = false;
        }
        check(rendered == (seconds >= 0 && seconds <= LAST_INSTANT), "rendering does not reject exactly the out-of-range instants");
        if (rendered)
        {
            check_rendering(seconds);
        }
    }
    return 0;
}

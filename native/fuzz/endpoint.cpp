// libFuzzer target: web endpoint validation. Authored and unrun.
// Input: one byte whose low bit grants plain-HTTP consent, then the address text.
// Invariants for accepted endpoints: bounded length, no spaces or control characters, no query, fragment or
// credentials, an https scheme (or http only with consent), and acceptance is monotonic in that consent.
#include "common.hpp"

#include <cstddef>
#include <cstdint>
#include <string>

using switchboard::Error;
using switchboard::fuzz::check;

namespace
{
bool accepted(const std::string &url, const bool allow_http)
{
    try
    {
        switchboard::validate_endpoint(url, allow_http);
        return true;
    }
    catch (const Error &error)
    {
        check(error.code() == "invalid", "endpoint rejection used an unexpected error code");
        return false;
    }
}
}  // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    if (size == 0)
    {
        return 0;
    }
    const bool allow_http = (data[0] & 1U) != 0;
    const auto url = switchboard::fuzz::text(data + 1, size - 1);
    if (!accepted(url, allow_http))
    {
        return 0;
    }
    check(!url.empty() && url.size() <= 2048, "accepted endpoint length is out of bounds");
    for (const auto character : url)
    {
        const auto byte = static_cast<unsigned char>(character);
        check(byte > 0x20 && byte != 0x7F, "accepted endpoint contains a space or control character");
    }
    check(url.find_first_of("?#@") == std::string::npos, "accepted endpoint carries a query, fragment or credentials");
    const auto lower = switchboard::fuzz::lowercase(url);
    check(lower.rfind("https:", 0) == 0 || (allow_http && lower.rfind("http:", 0) == 0), "accepted endpoint scheme is not permitted");
    check(allow_http || accepted(url, true), "granting plain-HTTP consent rejected an accepted endpoint");
    return 0;
}

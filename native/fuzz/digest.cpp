// libFuzzer target: HTTP digest challenge validation. Authored and unrun.
// Input: the pinned realm on the first line (1 to 256 bytes, as the transport requires), then the header.
// Invariants for accepted challenges: the documented shape, the pinned realm, SHA-256 and qop "auth".
#include "common.hpp"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>

using switchboard::Error;
using switchboard::fuzz::check;

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    const auto input = switchboard::fuzz::text(data, size);
    const auto newline = input.find('\n');
    if (newline == std::string::npos || newline == 0 || newline > 256)
    {
        return 0;
    }
    const auto realm = input.substr(0, newline);
    const auto header = input.substr(newline + 1);
    try
    {
        switchboard::validate_digest_challenge(header, realm);
    }
    catch (const Error &error)
    {
        check(error.code() == "devices_auth", "digest rejection used an unexpected error code");
        return 0;
    }
    check(header.size() >= 8 && header.size() <= 2048, "accepted challenge length is out of bounds");
    check(std::all_of(header.begin(), header.end(),
                      [](const char character)
                      {
                          const auto byte = static_cast<unsigned char>(character);
                          return byte >= 32 && byte <= 126;
                      }),
          "accepted challenge contains a nonprintable byte");
    check(switchboard::fuzz::lowercase(header.substr(0, 7)) == "digest ", "accepted challenge is not a Digest challenge");
    check(header.find('\\') == std::string::npos, "accepted challenge contains an escape");
    check(header.find(realm) != std::string::npos, "accepted challenge does not carry the pinned realm");
    check(switchboard::fuzz::lowercase(header).find("sha-256") != std::string::npos, "accepted challenge does not require SHA-256");
    check(header.find("auth") != std::string::npos, "accepted challenge does not use qop auth");
    return 0;
}

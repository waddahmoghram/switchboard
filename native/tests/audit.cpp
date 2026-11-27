#include "support.hpp"

#include "../src/audit_internal.hpp"

#include <chrono>
#include <cstddef>
#include <iostream>
#include <string>
#include <string_view>
#include <vector>

using namespace switchboard;
using switchboard::test::expect;
using switchboard::test::expect_error_code;

// The shared redaction rules are demonstrated by contracts/cases/audit-redaction.json; these cases cover what the
// fixtures cannot: running time on adversarial input and the native nesting limit.
namespace
{
constexpr std::size_t INPUT_BYTES = std::size_t{1} << 20;
// A linear scan of each input takes well under a second even under sanitizers; a scanner that rescans the rest of
// the text from each start position would take hours.
constexpr double SECONDS_PER_INPUT = 30.0;

std::string repeated(const std::string_view head, const std::string_view piece, const std::size_t bytes = INPUT_BYTES)
{
    std::string text(head);
    text.reserve(bytes + piece.size());
    while (text.size() < bytes)
    {
        text.append(piece);
    }
    return text;
}

struct Adversarial
{
    const char *family;
    std::string text;
};

// Each input starts a match attempt at nearly every position, or makes one attempt look far ahead and fail.
std::vector<Adversarial> adversarial_inputs()
{
    return {{"private key: headers without a label", repeated("", "-----BEGIN ")},
            {"private key: one header, near-miss footers", repeated("-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY---- ")},
            {"private key: long labels", repeated("", "-----BEGIN RSA PRIVATE KEY ")},
            {"jwt: one long header", repeated("", "eyJ")},
            {"jwt: short segments", repeated("", "eyJa.")},
            {"jwt: headers without a payload", repeated("", "eyJa.e.")},
            {"userinfo: schemes", repeated("", "a://")},
            {"userinfo: users without passwords", repeated("", "a://x:")},
            {"userinfo: dotted schemes", repeated("", "a.")},
            {"userinfo: one long user", repeated("a://", "x")},
            {"query: separators", repeated("", "?")},
            {"query: names without values", repeated("", "&access_token")},
            {"assignment: quotes", repeated("", "password:\"")},
            {"assignment: one unclosed quote", repeated("pin=\"", "x")},
            {"assignment: unclosed quotes and empty values", repeated("pin=\",", "pin=',")},
            {"assignment: labels", repeated("", "token=")},
            {"header: cookies without a pair", repeated("", "cookie:")},
            {"header: set-cookies without a pair", repeated("", "Set-Cookie:")},
            {"header: authorization", repeated("", "Authorization:")},
            {"header: quoted authorization", repeated("", "\"authorization:")},
            {"header: escapes", repeated("Authorization: \"", "\\\"")},
            {"bearer: keywords", repeated("", "bearer ")},
            {"bearer: short tokens", repeated("", "Bearer abcdefghijklmno ")},
            {"github: prefixes", repeated("", "ghp_")},
            {"github: fine-grained prefixes", repeated("", "github_pat_")},
            {"slack: prefixes", repeated("", "xoxb-")},
            {"aws: prefixes", repeated("", "AKIA")},
            {"api key: prefixes", repeated("", "sk_live_")},
            {"api key: one long key", repeated("sk-", "a")},
            {"telegram: bot identifiers", repeated("", "123456:")},
            {"telegram: prefixes", repeated("", "bot")},
            {"hex: one long run", repeated("", "0123456789abcdef")},
            {"base64url: one long run", repeated("", "aB3-")},
            {"base64url: long capital runs", repeated("", "ABCDEFGHIJ1")}};
}

void adversarial_input_is_linear()
{
    for (const auto &input : adversarial_inputs())
    {
        const auto started = std::chrono::steady_clock::now();
        const auto scrubbed = audit::scrub(input.text, false);
        const std::chrono::duration<double> elapsed = std::chrono::steady_clock::now() - started;
        std::cout << input.family << ": " << input.text.size() << " bytes in " << elapsed.count() << " s\n";
        expect(!scrubbed.empty(), std::string("native audit scrubbing lost its input: ") + input.family);
        expect(elapsed.count() < SECONDS_PER_INPUT, std::string("native audit scrubbing is not linear: ") + input.family);
    }
}

Json nested(const std::size_t depth)
{
    Json value = "leaf";
    for (std::size_t level = 0; level < depth; ++level)
    {
        value = Json::array({value});
    }
    return value;
}

void nesting_limit()
{
    expect(audit::redact(nested(audit::MAX_DEPTH)).is_array(), "native audit redaction refused a value at its nesting limit");
    expect_error_code(
        []
        {
            (void)audit::redact(nested(audit::MAX_DEPTH + 1));
        },
        "limit", "native audit redaction accepted a value beyond its nesting limit");
}
}  // namespace

int main()
{
    adversarial_input_is_linear();
    nesting_limit();
    return 0;
}

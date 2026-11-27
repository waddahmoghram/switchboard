#pragma once

#include "switchboard/native.hpp"

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

// Audit redaction shared by the runtime and the tests. It ports redact and _scrub from src/switchboard/audit.py
// with hand-written scanners, without the Python structural limits; contracts/README.md, "Audit redaction", is
// the shared rule.
namespace switchboard::audit
{
constexpr std::size_t MAX_HASH_ONLY = 64;
// Nesting deeper than this is refused with a "limit" error rather than abbreviated.
constexpr std::size_t MAX_DEPTH = 64;

// Validates an audit configuration section's hash_only list (missing means none) and returns its names in ASCII
// lower case.
[[nodiscard]] std::vector<std::string> hash_only_names(const Json &section);

// Replaces recognizable secrets in text; identifier keeps long hexadecimal and base64url runs.
[[nodiscard]] std::string scrub(std::string_view text, bool identifier);

// Returns a copy of value that is safe to persist: values under credential-like keys become "[redacted]", strings
// are scrubbed, and strings at any depth under a key named in hash_only (case-insensitive) keep only their length
// and a short hash.
[[nodiscard]] Json redact(const Json &value, const std::vector<std::string> &hash_only = {});
}  // namespace switchboard::audit

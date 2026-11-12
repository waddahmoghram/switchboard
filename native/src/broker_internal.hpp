#pragma once

#include "switchboard/native.hpp"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

// Framing, authorization and dispatch pieces of the native broker. They are separate from the socket loop so
// tests can inject peer credentials and request lines without creating accounts or listening sockets.
namespace switchboard::broker
{
// A request line is bounded like every other native request. A reply embeds a report of at most 8 MiB,
// which protocol text escaping can at most double; the reply bound leaves room for that.
constexpr std::size_t MAX_LINE_BYTES = MAX_REQUEST_BYTES;
constexpr std::size_t MAX_RESPONSE_BYTES = 32 * MAX_REQUEST_BYTES;
constexpr std::size_t MAX_CLIENT_CONNECTIONS = 16;
constexpr std::size_t MAX_OPERATOR_CONNECTIONS = 4;
constexpr std::size_t MAX_ACCOUNTS = 64;

enum class Role : std::uint8_t
{
    client,    // assistant protocol only: tool listing and tool calls
    operator_  // approvals, journal reconciliation and the kill switch; never reachable from the client socket
};

[[nodiscard]] const char *role_name(Role role) noexcept;

// Splits a byte stream into newline-terminated lines. Every line, complete or still arriving, must fit the
// bound; once one does not, the buffer is overflowed for good and the connection must close.
class LineBuffer
{

public:
    LineBuffer() noexcept = default;
    explicit LineBuffer(std::size_t maximum) noexcept;
    [[nodiscard]] bool append(std::string_view bytes);
    // Removes and returns the next complete line, without its terminator.
    [[nodiscard]] std::optional<std::string> next();
    // At end of input, once no complete line remains: removes and returns the unterminated rest, if any.
    [[nodiscard]] std::optional<std::string> finish();
    [[nodiscard]] bool ready() const noexcept;
    [[nodiscard]] bool overflowed() const noexcept;

private:
    std::string data_;
    std::size_t maximum_ = MAX_LINE_BYTES;
    std::size_t scanned_ = 0;
    std::size_t line_start_ = 0;
    bool overflow_ = false;
};

// Measures line lengths in a byte stream that is relayed without holding whole lines.
class LineMeter
{

public:
    explicit LineMeter(std::size_t maximum) noexcept;
    // False once any line exceeds the maximum; the relay must stop.
    [[nodiscard]] bool add(std::string_view bytes) noexcept;

private:
    std::size_t maximum_;
    std::size_t current_ = 0;
    bool exceeded_ = false;
};

// Rejects relative, unnormalized, overlong or identical socket paths and empty, oversized or reserved user lists.
void check_options(const BrokerOptions &options);
// True only when the operating system reported a peer user identifier listed for this socket's role.
[[nodiscard]] bool authorized(Role role, std::optional<std::uint32_t> uid, const BrokerOptions &options) noexcept;
[[nodiscard]] bool peer_credentials_supported() noexcept;
// The effective user identifier the connected peer had when it connected; nullopt when unavailable.
[[nodiscard]] std::optional<std::uint32_t> peer_uid(int descriptor) noexcept;

// Runs one validated operator request of the shared protocol in contracts/README.md: {"op": "status" |
// "approvals" | "approval" | "approve" | "deny" | "actions" | "reconcile" | "kill_switch", "request_id"?, ...}.
// Throws Error for malformed requests, unknown approvals and an unreadable status.
[[nodiscard]] Json operator_result(Runtime &runtime, const Json &request);
// Answers one request line from an admitted peer of the given role. Returns the framed response line, or nullopt
// for blank lines and protocol notifications. Failures become bounded error responses; only allocation
// failures propagate.
[[nodiscard]] std::optional<std::string> respond(Runtime &runtime, Role role, const std::string &line);
// Serializes one response line ending in a newline; an oversized response becomes a bounded error response.
[[nodiscard]] std::string frame(const Json &value, Role role, std::size_t maximum = MAX_RESPONSE_BYTES);
// The final response sent before closing a connection whose request line exceeded the bound.
[[nodiscard]] std::string overflow_response(Role role);
}  // namespace switchboard::broker

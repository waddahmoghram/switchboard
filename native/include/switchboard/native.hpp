#pragma once

#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace switchboard
{
using Json = nlohmann::json;
using Clock = std::function<std::int64_t()>;
constexpr std::size_t MAX_REQUEST_BYTES = 1024 * 1024;
constexpr std::int64_t MAX_JSON_INTEGER = 9007199254740991LL;
// State schema written by this engine. Version 1 is migrated forward in one transaction; newer or
// incomplete state is refused instead of being downgraded or silently recreated.
constexpr std::int64_t STATE_SCHEMA_VERSION = 2;

enum class Tier : std::uint8_t
{
    read,
    draft,
    act,
    spend
};

enum class Origin : std::uint8_t
{
    assistant,
    rule,
    human
};

class Error : public std::runtime_error
{

public:
    Error(std::string code, std::string message);
    [[nodiscard]] const std::string &code() const noexcept;

private:
    std::string code_;
};

struct HttpRequest
{
    std::string method = "GET";
    std::string url;
    std::map<std::string, std::string> headers;
    std::string body;
    std::size_t max_bytes = 65536;
    std::int64_t timeout_seconds = 5;
    bool allow_http = false;
    std::optional<std::string> digest_password;
    std::optional<std::string> digest_realm;
};

struct HttpResponse
{
    long status = 0;
    std::map<std::string, std::string> headers;
    std::string body;
};

class HttpTransport
{

public:
    virtual ~HttpTransport() = default;
    virtual HttpResponse request(const HttpRequest &request) = 0;
};

[[nodiscard]] std::unique_ptr<HttpTransport> make_http_transport();

// Per-thread limits for the operation currently executing. The runtime installs them around every tool
// handler and polling job; the web transport clamps its timeout to the remaining time and aborts when the
// deadline passes or cancellation is requested. Adapters do not need to pass them explicitly.
struct ExecutionLimits
{
    std::chrono::steady_clock::time_point deadline;
    const std::atomic<std::uint64_t> *cancel_generation = nullptr;
    std::uint64_t started_generation = 0;

    [[nodiscard]] bool cancelled() const noexcept;
    [[nodiscard]] bool expired() const noexcept;
    [[nodiscard]] std::int64_t remaining_seconds() const noexcept;
};

class ScopedExecutionLimits
{

public:
    explicit ScopedExecutionLimits(ExecutionLimits limits);
    ~ScopedExecutionLimits();
    ScopedExecutionLimits(const ScopedExecutionLimits &) = delete;
    ScopedExecutionLimits &operator=(const ScopedExecutionLimits &) = delete;

private:
    std::optional<ExecutionLimits> previous_;
};

[[nodiscard]] std::optional<ExecutionLimits> current_execution_limits() noexcept;

enum class DatabaseMode : std::uint8_t
{
    read_write,
    // Opens an existing private rollback-journal copy, such as one written by backup_to, without creating,
    // migrating or writing it. Used to verify backups.
    read_only
};

class Database
{

public:
    explicit Database(const std::filesystem::path &path);
    Database(const std::filesystem::path &path, DatabaseMode mode);
    ~Database();
    Database(const Database &) = delete;
    Database &operator=(const Database &) = delete;
    std::int64_t execute(const std::string &sql, const std::vector<Json> &parameters = {});
    [[nodiscard]] std::vector<Json> query(const std::string &sql, const std::vector<Json> &parameters = {});
    [[nodiscard]] std::int64_t last_insert_id() const;
    void begin();
    void commit();
    void rollback() noexcept;
    // Consistent online copy through the SQLite backup interface into a new private (0600) file, left in
    // rollback-journal mode so it can be opened read-only. An existing destination is refused.
    void backup_to(const std::filesystem::path &destination);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

class Transaction
{

public:
    explicit Transaction(Database &database);
    ~Transaction();
    Transaction(const Transaction &) = delete;
    Transaction &operator=(const Transaction &) = delete;
    void commit();

private:
    Database &database_;
    bool active_ = true;
};

struct Job
{
    std::string name;
    std::int64_t interval_seconds;
    std::function<void()> handler;
    std::int64_t next_run = 0;
    std::optional<std::string> error;
    // Runner bookkeeping: failures back off exponentially (capped) and reset on success.
    std::int64_t consecutive_failures = 0;
    std::int64_t overruns = 0;
    std::optional<std::int64_t> last_started_at;
    std::optional<std::int64_t> last_duration_ms;
};

class Context
{

public:
    Context(const Json &configuration, std::filesystem::path data_directory, Database &database, HttpTransport &transport, Clock clock);
    [[nodiscard]] bool enabled(const std::string &name) const;
    [[nodiscard]] Json settings(const std::string &name) const;
    [[nodiscard]] std::string secret(const std::string &reference) const;
    [[nodiscard]] std::int64_t now() const;
    [[nodiscard]] std::string timestamp() const;
    void emit(const std::string &source, const std::string &type, const Json &payload, const std::optional<std::string> &dedupe_key = std::nullopt);
    void add_job(std::string name, std::int64_t interval_seconds, std::function<void()> handler);

    const Json &configuration;
    std::filesystem::path data_dir;
    Database &db;
    HttpTransport &http;
    std::vector<Job> jobs;

private:
    Clock clock_;
};

struct Tool
{
    std::string name;
    std::string description;
    Tier tier;
    Json schema;
    std::function<Json(const Json &)> handler;
    std::function<std::string(const Json &)> summary = {};
};

class Registry
{

public:
    void add(Tool tool);
    [[nodiscard]] const Tool *find(const std::string &name) const;
    [[nodiscard]] const std::map<std::string, Tool> &tools() const noexcept;

private:
    std::map<std::string, Tool> tools_;
};

[[nodiscard]] std::string tier_name(Tier tier);
[[nodiscard]] std::string hash(const Json &value);
[[nodiscard]] Json parse_json(const std::string &text, std::size_t maximum = MAX_REQUEST_BYTES);
void check_schema(const Json &schema);
void validate(const Json &value, const Json &schema);
void check_object(const Json &value, const std::vector<std::string> &allowed, const std::vector<std::string> &required = {});
[[nodiscard]] std::string string_field(const Json &value, const std::string &key, std::size_t maximum, bool required = true, std::size_t minimum = 0);
[[nodiscard]] std::int64_t integer_field(const Json &value, const std::string &key, std::int64_t minimum, std::int64_t maximum, std::int64_t fallback);
[[nodiscard]] bool boolean_field(const Json &value, const std::string &key, bool fallback = false);
[[nodiscard]] Json object_schema(const Json &properties, const std::vector<std::string> &required = {});
[[nodiscard]] std::string timestamp(std::int64_t seconds);
[[nodiscard]] std::int64_t parse_timestamp(const std::string &value);
[[nodiscard]] std::filesystem::path expand_path(const std::string &value);
void ensure_private_directory(const std::filesystem::path &path);
[[nodiscard]] std::string read_private_file(const std::filesystem::path &root, const std::string &filename, std::size_t maximum, bool secret = false);
[[nodiscard]] std::vector<std::string> private_files(const std::filesystem::path &root, std::size_t maximum);
// SHA-256 (lowercase hexadecimal) of an owned private regular file in a private directory, without following links.
[[nodiscard]] std::string sha256_file(const std::filesystem::path &root, const std::string &filename, std::uint64_t maximum);
// Creates a new private (0600) file in a private directory, writes all content and syncs it; existing files are refused.
void write_private_file(const std::filesystem::path &root, const std::string &filename, const std::string &content);
void validate_endpoint(const std::string &url, bool allow_http);
void validate_digest_challenge(const std::string &header, const std::string &expected_realm);
[[nodiscard]] Json response_json(const HttpResponse &response);

void register_home(Context &context, Registry &registry);
void register_devices(Context &context, Registry &registry);
void register_personal(Context &context, Registry &registry);
void register_monitoring(Context &context, Registry &registry);

// Trusted in-process registration for C++ embedders and contract tests. Tools added here still pass the
// same schema, policy, approval, journal and audit path as built-in tools.
using Extension = std::function<void(Context &, Registry &)>;

class Runtime
{

public:
    explicit Runtime(Json configuration, std::unique_ptr<HttpTransport> transport = {}, Clock clock = {}, Extension extension = {});
    ~Runtime();
    Runtime(const Runtime &) = delete;
    Runtime &operator=(const Runtime &) = delete;
    [[nodiscard]] Json catalog() const;
    [[nodiscard]] Json call(const std::string &name, const Json &arguments, Origin origin = Origin::assistant);
    [[nodiscard]] Json approve(const std::string &id);
    [[nodiscard]] Json deny(const std::string &id);
    [[nodiscard]] Json tick();
    [[nodiscard]] Json rpc(const Json &message);

    // Operator interface: journal entries ({"state"?, "limit"?} or {"id"} for one entry with arguments).
    [[nodiscard]] Json actions(const Json &filter);
    // Operator review of one approval, including its exact stored arguments; throws Error("invalid") when the
    // identifier is malformed or unknown. Approving executes exactly these arguments.
    [[nodiscard]] Json approval(const std::string &id);
    // resolution is "applied" or "not_applied"; only uncertain entries can be reconciled.
    [[nodiscard]] Json reconcile(const std::string &id, const std::string &resolution, const std::string &note);
    // Thread-safe and lock-free: aborts web transfers of the operation in progress, if any.
    void cancel() noexcept;
    // nullopt reports the state; true or false sets it.
    [[nodiscard]] Json kill_switch(std::optional<bool> enable);
    [[nodiscard]] Json backup(const std::filesystem::path &destination);
    [[nodiscard]] Json purge(bool dry_run);
    [[nodiscard]] static Json verify_backup(const std::filesystem::path &source);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};

struct BrokerOptions
{
    std::filesystem::path client_socket;
    std::filesystem::path operator_socket;
    std::vector<std::uint32_t> client_uids;
    std::vector<std::uint32_t> operator_uids;
};

// Serve assistant protocol requests and operator requests on separate peer-checked local sockets.
int run_broker(Runtime &runtime, const BrokerOptions &options);
// Forward standard input/output protocol lines to a broker client socket; holds no configuration or secrets.
int run_broker_shim(const std::filesystem::path &client_socket);
}  // namespace switchboard

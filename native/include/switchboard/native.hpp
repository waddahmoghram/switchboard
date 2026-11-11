#pragma once

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

class Database
{

public:
    explicit Database(const std::filesystem::path &path);
    ~Database();
    Database(const Database &) = delete;
    Database &operator=(const Database &) = delete;
    std::int64_t execute(const std::string &sql, const std::vector<Json> &parameters = {});
    [[nodiscard]] std::vector<Json> query(const std::string &sql, const std::vector<Json> &parameters = {});
    [[nodiscard]] std::int64_t last_insert_id() const;
    void begin();
    void commit();
    void rollback() noexcept;

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
void validate_endpoint(const std::string &url, bool allow_http);
void validate_digest_challenge(const std::string &header, const std::string &expected_realm);
[[nodiscard]] Json response_json(const HttpResponse &response);

void register_home(Context &context, Registry &registry);
void register_devices(Context &context, Registry &registry);
void register_personal(Context &context, Registry &registry);
void register_monitoring(Context &context, Registry &registry);

class Runtime
{

public:
    explicit Runtime(Json configuration, std::unique_ptr<HttpTransport> transport = {}, Clock clock = {});
    ~Runtime();
    Runtime(const Runtime &) = delete;
    Runtime &operator=(const Runtime &) = delete;
    [[nodiscard]] Json catalog() const;
    [[nodiscard]] Json call(const std::string &name, const Json &arguments, Origin origin = Origin::assistant);
    [[nodiscard]] Json approve(const std::string &id);
    [[nodiscard]] Json deny(const std::string &id);
    [[nodiscard]] Json tick();
    [[nodiscard]] Json rpc(const Json &message);

private:
    class Impl;
    std::unique_ptr<Impl> impl_;
};
}  // namespace switchboard

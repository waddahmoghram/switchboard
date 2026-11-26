#include "switchboard/native.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cmath>
#include <regex>
#include <set>
#include <string_view>
#include <utility>

#include <arpa/inet.h>

namespace switchboard
{
namespace
{
constexpr std::size_t RESPONSE_BYTES = 65536;
constexpr std::size_t REPORT_BYTES = 16384;
constexpr std::int64_t LAST_SECOND = 253402300799LL;
constexpr std::int64_t MICROSECONDS = 1000000;

struct Check
{
    std::string name;
    std::string kind;
    Json settings;
    std::string config_key;
};

struct Settings
{
    std::vector<Check> checks;
    std::int64_t poll_interval = 300;
    std::int64_t stale_after = 600;
};

struct Saved
{
    Json snapshot;
    std::int64_t checked_at = 0;
    bool changed = false;
    std::int64_t events_queued = 0;
};

bool printable(const std::string &value, const unsigned char minimum = 32)
{
    return std::all_of(value.begin(), value.end(),
                       [minimum](const unsigned char character)
                       {
                           return character >= minimum && character <= 126;
                       });
}

bool integer(const Json &value, const std::int64_t low, const std::int64_t high)
{
    if (!value.is_number_integer())
    {
        return false;
    }
    if (value.is_number_unsigned())
    {
        return value.get<std::uint64_t>() <= static_cast<std::uint64_t>(high) && (low <= 0 || value.get<std::uint64_t>() >= static_cast<std::uint64_t>(low));
    }
    const auto number = value.get<std::int64_t>();
    return number >= low && number <= high;
}

std::int64_t number(const Json &object, const std::string &key, const std::int64_t low, const std::int64_t high,
                    const std::optional<std::int64_t> fallback = std::nullopt)
{
    if (!object.contains(key) && fallback)
    {
        return *fallback;
    }
    if (!object.contains(key) || !integer(object.at(key), low, high))
    {
        throw Error("monitoring_config", "monitoring: invalid integer setting");
    }
    return object.at(key).get<std::int64_t>();
}

std::string absolute_path(const Json &value)
{
    if (!value.is_string())
    {
        throw Error("monitoring_config", "monitoring: a configured absolute local path is required");
    }
    const auto text = value.get<std::string>();
    if (text.empty() || text.size() > 4096 || text.front() != '/' || text.starts_with("//") || text.find('\\') != std::string::npos || !printable(text))
    {
        throw Error("monitoring_config", "monitoring: invalid absolute local path");
    }
    const std::filesystem::path path(text);
    for (const auto &part : path)
    {
        if (part == "." || part == "..")
        {
            throw Error("monitoring_config", "monitoring: dot path segments are forbidden");
        }
    }
    auto normalized = path.lexically_normal().string();
    while (normalized.size() > 1 && normalized.back() == '/')
    {
        normalized.pop_back();
    }
    return normalized;
}

void endpoint_address(const std::string &url, const bool allow_http)
{
    if (url.empty() || url.size() > 2048 || !printable(url, 33) || url.find_first_of("\\@?#%") != std::string::npos ||
        (!url.starts_with("https://") && !url.starts_with("http://")))
    {
        throw Error("monitoring_config", "monitoring: invalid fixed endpoint address");
    }
    const auto prefix = url.starts_with("https://") ? 8U : 7U;
    const auto slash = url.find('/', prefix);
    const auto authority = url.substr(prefix, slash == std::string::npos ? slash : slash - prefix);
    const auto path = slash == std::string::npos ? std::string() : url.substr(slash);
    if (!path.empty())
    {
        static const std::regex path_pattern("/[a-zA-Z0-9_./~-]*");
        if (!std::regex_match(path, path_pattern))
        {
            throw Error("monitoring_config", "monitoring: invalid endpoint path");
        }
        std::size_t start = 1;
        while (start <= path.size())
        {
            const auto end = path.find('/', start);
            const auto part = path.substr(start, end == std::string::npos ? end : end - start);
            if (part == "." || part == "..")
            {
                throw Error("monitoring_config", "monitoring: dot endpoint segments are forbidden");
            }
            if (end == std::string::npos)
            {
                break;
            }
            start = end + 1;
        }
    }

    std::string host;
    std::string port;
    bool has_port = false;
    bool local = false;
    bool invalid = authority.empty();
    if (authority.starts_with("["))
    {
        const auto close = authority.find(']');
        invalid = close == std::string::npos;
        if (!invalid)
        {
            host = authority.substr(1, close - 1);
            const auto rest = authority.substr(close + 1);
            has_port = !rest.empty();
            invalid = has_port && rest.front() != ':';
            port = has_port ? rest.substr(1) : std::string();
            std::array<unsigned char, 16> address{};
            if (inet_pton(AF_INET6, host.c_str(), address.data()) != 1)
            {
                invalid = true;
            }
            else
            {
                const bool zero_prefix = std::all_of(address.begin(), address.end() - 1,
                                                     [](const unsigned char byte)
                                                     {
                                                         return byte == 0;
                                                     });
                invalid = invalid || (zero_prefix && address.back() == 0) || address.front() == 0xff;
                local = (zero_prefix && address.back() == 1) || (address.front() & 0xfe) == 0xfc;
            }
        }
    }
    else
    {
        const auto colon = authority.find(':');
        has_port = colon != std::string::npos;
        host = authority.substr(0, colon);
        port = has_port ? authority.substr(colon + 1) : std::string();
        std::transform(host.begin(), host.end(), host.begin(),
                       [](const unsigned char character)
                       {
                           return character >= 'A' && character <= 'Z' ? static_cast<char>(character + ('a' - 'A')) : static_cast<char>(character);
                       });
        std::array<unsigned char, 4> address{};
        if (inet_pton(AF_INET, host.c_str(), address.data()) == 1)
        {
            invalid = (address[0] == 0 && address[1] == 0 && address[2] == 0 && address[3] == 0) || (address[0] >= 224 && address[0] <= 239);
            local = address[0] == 127 || address[0] == 10 || (address[0] == 172 && address[1] >= 16 && address[1] <= 31) ||
                    (address[0] == 192 && address[1] == 168);
        }
        else
        {
            static const std::regex host_pattern("[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*");
            invalid = host.size() > 253 || !std::regex_match(host, host_pattern) ||
                      std::all_of(host.begin(), host.end(),
                                  [](const unsigned char character)
                                  {
                                      return (character >= '0' && character <= '9') || character == '.';
                                  });
            local = host == "localhost";
        }
    }
    if (has_port)
    {
        unsigned value = 0;
        const auto parsed = std::from_chars(port.data(), port.data() + port.size(), value);
        invalid = invalid || port.empty() || port.size() > 5 || parsed.ec != std::errc() || parsed.ptr != port.data() + port.size() || value == 0 ||
                  value > 65535 || std::to_string(value) != port;
    }
    if (invalid || (url.starts_with("http://") && (!allow_http || !local)))
    {
        throw Error("monitoring_config", "monitoring: invalid endpoint or unconsented non-private plain HTTP");
    }
    validate_endpoint(url, allow_http);
}

Settings load_settings(const Json &raw)
{
    check_object(raw, {"checks", "poll_interval_seconds", "snapshot_stale_after_seconds"}, {"checks"});
    Settings settings;
    settings.poll_interval = number(raw, "poll_interval_seconds", 30, 86400, 300);
    settings.stale_after = number(raw, "snapshot_stale_after_seconds", settings.poll_interval, 604800, std::max<std::int64_t>(600, settings.poll_interval * 2));
    const auto &checks = raw.at("checks");
    if (!checks.is_object() || checks.empty() || checks.size() > 32)
    {
        throw Error("monitoring_config", "monitoring: configure between 1 and 32 named checks");
    }
    static const std::regex name_pattern("[a-z][a-z0-9_]{0,63}");
    static const std::regex field_pattern("[a-zA-Z_][a-zA-Z0-9_]{0,31}(?:\\.[a-zA-Z_][a-zA-Z0-9_]{0,31}){0,7}");
    static const std::regex environment_pattern("env:[a-zA-Z_][a-zA-Z0-9_]{0,127}");
    static const std::regex filename_pattern("[a-zA-Z0-9][a-zA-Z0-9_.-]{0,122}\\.json");
    for (const auto &[name, value] : checks.items())
    {
        if (!std::regex_match(name, name_pattern) || !value.is_object())
        {
            throw Error("monitoring_config", "monitoring: invalid named check table");
        }
        const auto kind = string_field(value, "kind", 16, true, 1);
        Json normalized;
        if (kind == "endpoint")
        {
            check_object(value, {"kind", "url", "expected_status", "readiness_field", "readiness_equals", "bearer_token", "allow_http", "timeout_seconds"},
                         {"kind", "url"});
            const bool allow_http = boolean_field(value, "allow_http", false);
            const auto url = string_field(value, "url", 2048, true, 1);
            endpoint_address(url, allow_http);
            normalized = {{"kind", kind},
                          {"url", url},
                          {"allow_http", allow_http},
                          {"expected_status", number(value, "expected_status", 200, 299, 200)},
                          {"timeout_seconds", number(value, "timeout_seconds", 1, 30, 5)}};
            if (value.contains("readiness_field") != value.contains("readiness_equals"))
            {
                throw Error("monitoring_config", "monitoring: readiness field and equality must be paired");
            }
            if (value.contains("readiness_field"))
            {
                const auto field = string_field(value, "readiness_field", 255, true, 1);
                const auto &expected = value.at("readiness_equals");
                const bool text = expected.is_string() && !expected.get_ref<const std::string &>().empty() &&
                                  expected.get_ref<const std::string &>().size() <= 128 && printable(expected.get_ref<const std::string &>());
                if (!std::regex_match(field, field_pattern) || (!expected.is_boolean() && !integer(expected, -1000000000, 1000000000) && !text))
                {
                    throw Error("monitoring_config", "monitoring: invalid finite typed readiness criterion");
                }
                normalized["readiness_field"] = field;
                normalized["readiness_equals"] = expected;
            }
            if (value.contains("bearer_token"))
            {
                const auto reference = string_field(value, "bearer_token", 4096, true, 1);
                if (!std::regex_match(reference, environment_pattern))
                {
                    if (!reference.starts_with("file:"))
                    {
                        throw Error("monitoring_config", "monitoring: only env: or file: secret references are supported");
                    }
                    static_cast<void>(absolute_path(reference.substr(5)));
                }
                normalized["bearer_token"] = reference;
            }
        }
        else if (kind == "backup")
        {
            check_object(value, {"kind", "directory", "filename", "max_age_seconds"}, {"kind", "directory", "filename", "max_age_seconds"});
            const auto filename = string_field(value, "filename", 128, true, 1);
            if (!std::regex_match(filename, filename_pattern))
            {
                throw Error("monitoring_config", "monitoring: report filename must be a simple .json basename");
            }
            normalized = {{"kind", kind},
                          {"directory", absolute_path(value.at("directory"))},
                          {"filename", filename},
                          {"max_age_seconds", number(value, "max_age_seconds", 30, 31536000)}};
        }
        else if (kind == "disk")
        {
            check_object(value, {"kind", "path", "min_free_bytes", "min_free_percent"}, {"kind", "path"});
            normalized = {{"kind", kind}, {"path", absolute_path(value.at("path"))}};
            if (value.contains("min_free_bytes"))
            {
                normalized["min_free_bytes"] = number(value, "min_free_bytes", 1, MAX_JSON_INTEGER);
            }
            if (value.contains("min_free_percent"))
            {
                const auto &percent = value.at("min_free_percent");
                if (!percent.is_number() || !std::isfinite(percent.get<double>()) || percent.get<double>() <= 0 || percent.get<double>() > 100)
                {
                    throw Error("monitoring_config", "monitoring: invalid free-capacity percentage");
                }
                normalized["min_free_percent"] = percent.get<double>();
            }
            if (!normalized.contains("min_free_bytes") && !normalized.contains("min_free_percent"))
            {
                throw Error("monitoring_config", "monitoring: a disk check requires a capacity threshold");
            }
        }
        else
        {
            throw Error("monitoring_config", "monitoring: unsupported check kind");
        }
        const auto config_key = hash(Json{{"native_monitoring_schema", 1}, {"name", name}, {"check", normalized}});
        settings.checks.push_back({name, kind, std::move(normalized), config_key});
    }
    return settings;
}

Json observation(const std::string &status, Json error = nullptr)
{
    return {{"status", status}, {"error", std::move(error)},      {"reachable", nullptr},
            {"ready", nullptr}, {"measurements", Json::object()}, {"report", Json::object()}};
}

Json object_document(const std::string &body, const std::size_t maximum)
{
    if (body.size() > maximum)
    {
        throw Error("monitoring_document", "monitoring: document too large");
    }
    std::vector<std::set<std::string>> keys;
    const auto callback = [&keys](const int depth, const Json::parse_event_t event, Json &parsed)
    {
        if (depth > 32)
        {
            throw Error("monitoring_document", "monitoring: document nesting too deep");
        }
        if (event == Json::parse_event_t::object_start)
        {
            keys.emplace_back();
        }
        else if (event == Json::parse_event_t::key)
        {
            if (keys.empty() || !keys.back().insert(parsed.get<std::string>()).second)
            {
                throw Error("monitoring_document", "monitoring: duplicate document property");
            }
        }
        else if (event == Json::parse_event_t::object_end)
        {
            keys.pop_back();
        }
        if (event == Json::parse_event_t::value && parsed.is_number_float() && !std::isfinite(parsed.get<double>()))
        {
            throw Error("monitoring_document", "monitoring: non-finite document number");
        }
        return true;
    };
    auto document = Json::parse(body.begin(), body.end(), callback);
    if (!document.is_object())
    {
        throw Error("monitoring_document", "monitoring: a document object is required");
    }
    return document;
}

std::int64_t checked_now(Context &context)
{
    std::int64_t now = 0;
    try
    {
        now = context.now();
    }
    catch (...)
    {
        throw Error("monitoring_clock", "monitoring: observation clock is unavailable or outside its supported range");
    }
    if (now < 0 || now > LAST_SECOND)
    {
        throw Error("monitoring_clock", "monitoring: clock must be an epoch second in years 1970 through 9999");
    }
    return now;
}

std::int64_t report_time(const Json &value)
{
    if (!value.is_string())
    {
        throw Error("monitoring_time", "monitoring: invalid report time");
    }
    const auto text = value.get<std::string>();
    static const std::regex pattern("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                                    "(?:\\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])");
    if (!std::regex_match(text, pattern) || text.ends_with("-00:00"))
    {
        throw Error("monitoring_time", "monitoring: report timestamps require a known timezone");
    }
    const auto year = std::stoi(text.substr(0, 4));
    const auto month = static_cast<unsigned>(std::stoi(text.substr(5, 2)));
    const auto day = static_cast<unsigned>(std::stoi(text.substr(8, 2)));
    const std::chrono::year_month_day date{std::chrono::year{year}, std::chrono::month{month}, std::chrono::day{day}};
    if (year < 1970 || !date.ok() || std::stoi(text.substr(11, 2)) > 23 || std::stoi(text.substr(14, 2)) > 59 || std::stoi(text.substr(17, 2)) > 59)
    {
        throw Error("monitoring_time", "monitoring: invalid report calendar time");
    }
    auto seconds = parse_timestamp(text.substr(0, 19) + "Z");
    std::size_t zone = 19;
    std::int64_t fraction = 0;
    if (text[zone] == '.')
    {
        const auto begin = ++zone;
        while (zone < text.size() && text[zone] >= '0' && text[zone] <= '9')
        {
            ++zone;
        }
        auto digits = text.substr(begin, zone - begin);
        digits.append(6 - digits.size(), '0');
        fraction = std::stoll(digits);
    }
    if (text[zone] != 'Z')
    {
        const auto offset = std::stoll(text.substr(zone + 1, 2)) * 3600 + std::stoll(text.substr(zone + 4, 2)) * 60;
        seconds += text[zone] == '+' ? -offset : offset;
    }
    if (seconds < 0 || seconds > LAST_SECOND)
    {
        throw Error("monitoring_time", "monitoring: report instant outside supported range");
    }
    return seconds * MICROSECONDS + fraction;
}

std::string report_timestamp(const std::int64_t microseconds)
{
    const auto whole = timestamp(microseconds / MICROSECONDS);
    auto fraction = std::to_string(microseconds % MICROSECONDS);
    fraction.insert(0, 6 - fraction.size(), '0');
    return whole.substr(0, 19) + "." + fraction + "Z";
}

bool typed_equal(const Json &actual, const Json &expected)
{
    if (expected.is_boolean())
    {
        return actual.is_boolean() && actual == expected;
    }
    if (expected.is_string())
    {
        return actual.is_string() && actual == expected;
    }
    return integer(actual, -1000000000, 1000000000) && actual == expected;
}

Json endpoint_observation(Context &context, const Check &check)
{
    HttpRequest request;
    request.url = check.settings.at("url").get<std::string>();
    request.max_bytes = RESPONSE_BYTES;
    request.timeout_seconds = check.settings.at("timeout_seconds").get<std::int64_t>();
    request.allow_http = check.settings.at("allow_http").get<bool>();
    request.headers["Accept"] = "application/json";
    if (check.settings.contains("bearer_token"))
    {
        std::string token;
        try
        {
            token = context.secret(check.settings.at("bearer_token").get<std::string>());
        }
        catch (...)
        {
            return observation("error", "secret_unavailable");
        }
        if (token.empty() || token.size() > 4096 || !printable(token, 33))
        {
            return observation("error", "secret_invalid");
        }
        request.headers["Authorization"] = "Bearer " + token;
    }
    HttpResponse response;
    try
    {
        response = context.http.request(request);
    }
    catch (const Error &error)
    {
        if (error.code() == "limit")
        {
            auto result = observation("error", "response_too_large");
            result["reachable"] = true;
            if (check.settings.contains("readiness_field"))
            {
                result["ready"] = false;
            }
            return result;
        }
        auto result = observation("error", "endpoint_unreachable");
        result["reachable"] = false;
        return result;
    }
    catch (...)
    {
        auto result = observation("error", "endpoint_unreachable");
        result["reachable"] = false;
        return result;
    }
    auto result = observation("unhealthy");
    result["reachable"] = true;
    const bool readiness = check.settings.contains("readiness_field");
    if (readiness)
    {
        result["ready"] = false;
    }
    if (response.status < 100 || response.status > 599)
    {
        result["status"] = "error";
        result["error"] = "endpoint_response_invalid";
        return result;
    }
    result["measurements"] = {{"http_status", response.status}, {"response_bytes", response.body.size()}};
    if (response.body.size() > RESPONSE_BYTES)
    {
        result["status"] = "error";
        result["error"] = "response_too_large";
    }
    else if (response.status >= 300 && response.status <= 399)
    {
        result["error"] = "redirect_refused";
    }
    else if (response.status != check.settings.at("expected_status").get<long>())
    {
        result["error"] = "unexpected_http_status";
    }
    else if (!readiness)
    {
        result["status"] = "reachable";
    }
    else
    {
        Json document;
        try
        {
            document = object_document(response.body, RESPONSE_BYTES);
        }
        catch (...)
        {
            result["status"] = "error";
            result["error"] = "response_malformed";
            return result;
        }
        const auto field = check.settings.at("readiness_field").get<std::string>();
        const Json *value = &document;
        std::size_t start = 0;
        while (start < field.size())
        {
            const auto end = field.find('.', start);
            const auto part = field.substr(start, end == std::string::npos ? end : end - start);
            if (!value->is_object() || !value->contains(part))
            {
                result["error"] = "readiness_field_missing";
                return result;
            }
            value = &value->at(part);
            if (end == std::string::npos)
            {
                break;
            }
            start = end + 1;
        }
        if (!typed_equal(*value, check.settings.at("readiness_equals")))
        {
            result["error"] = "readiness_mismatch";
        }
        else
        {
            result["status"] = "healthy";
            result["ready"] = true;
        }
    }
    return result;
}

Json backup_observation(const Check &check, const std::string &body, const std::int64_t checked_at)
{
    Json document;
    try
    {
        document = object_document(body, REPORT_BYTES);
    }
    catch (...)
    {
        return observation("error", "report_malformed");
    }
    try
    {
        check_object(document, {"schema_version", "check", "status", "started_at", "finished_at", "bytes_written"},
                     {"schema_version", "check", "status", "started_at", "finished_at"});
    }
    catch (...)
    {
        return observation("error", "report_schema_invalid");
    }
    if (!integer(document.at("schema_version"), 1, 1) || !document.at("check").is_string() || !document.at("status").is_string() ||
        (document.at("status") != Json("succeeded") && document.at("status") != Json("failed")) ||
        (document.contains("bytes_written") && !integer(document.at("bytes_written"), 0, MAX_JSON_INTEGER)))
    {
        return observation("error", "report_schema_invalid");
    }
    if (document.at("check") != check.name)
    {
        return observation("error", "report_check_mismatch");
    }
    std::int64_t started = 0;
    std::int64_t finished = 0;
    try
    {
        started = report_time(document.at("started_at"));
        finished = report_time(document.at("finished_at"));
    }
    catch (...)
    {
        return observation("error", "report_time_invalid");
    }
    if (started > finished || finished > checked_at * MICROSECONDS)
    {
        return observation("error", "report_time_invalid");
    }
    const auto max_age = check.settings.at("max_age_seconds").get<std::int64_t>();
    const auto age = checked_at * MICROSECONDS - finished;
    auto result = observation("healthy");
    result["measurements"] = {{"age_seconds", static_cast<double>(age) / MICROSECONDS},
                              {"duration_seconds", static_cast<double>(finished - started) / MICROSECONDS},
                              {"max_age_seconds", max_age}};
    if (document.contains("bytes_written"))
    {
        result["measurements"]["bytes_written"] = document.at("bytes_written");
    }
    result["report"] = {{"status", document.at("status")}, {"started_at", report_timestamp(started)}, {"finished_at", report_timestamp(finished)}};
    if (age > max_age * MICROSECONDS)
    {
        result["status"] = "stale";
        result["error"] = "backup_report_stale";
    }
    else if (document.at("status") == Json("failed"))
    {
        result["status"] = "unhealthy";
        result["error"] = "backup_failed";
    }
    return result;
}
}  // namespace

namespace monitoring_detail
{
// Pure capacity classification is shared with authored tests; it never measures a filesystem.
Json disk_capacity(const std::uintmax_t capacity, const std::uintmax_t free, const std::uintmax_t available, const std::optional<std::int64_t> min_bytes,
                   const std::optional<double> min_percent)
{
    const auto maximum = static_cast<std::uintmax_t>(MAX_JSON_INTEGER);
    if (capacity == 0 || capacity > maximum || free > capacity || available > free)
    {
        return observation("error", "disk_data_invalid");
    }
    const auto exact_percent = static_cast<long double>(available) * 100 / capacity;
    const auto percent = static_cast<double>(exact_percent);
    const bool low =
        (min_bytes && available < static_cast<std::uintmax_t>(*min_bytes)) || (min_percent && exact_percent < static_cast<long double>(*min_percent));
    auto result = observation(low ? "unhealthy" : "healthy", low ? Json("disk_below_threshold") : Json(nullptr));
    result["measurements"] = {{"total_bytes", capacity}, {"used_bytes", capacity - free}, {"free_bytes", available}, {"free_percent", percent}};
    if (min_bytes)
    {
        result["measurements"]["min_free_bytes"] = *min_bytes;
    }
    if (min_percent)
    {
        result["measurements"]["min_free_percent"] = *min_percent;
    }
    return result;
}
}  // namespace monitoring_detail

namespace
{
class Monitoring
{
public:
    Monitoring(Context &context, Settings settings) : context_(context), settings_(std::move(settings))
    {
        context_.db.execute("CREATE TABLE IF NOT EXISTS native_monitoring_snapshots ("
                            "name TEXT PRIMARY KEY, config_key TEXT NOT NULL, state_key TEXT NOT NULL, "
                            "snapshot_json TEXT NOT NULL, checked_at INTEGER NOT NULL, changed_at INTEGER NOT NULL, "
                            "revision INTEGER NOT NULL CHECK (revision >= 1))");
    }

    Json status(const Json &arguments)
    {
        const auto targets = select(arguments, false);
        const auto now = checked_now(context_);
        auto snapshots = Json::array();
        for (const auto *check : targets)
        {
            const auto rows = context_.db.query("SELECT snapshot_json, checked_at FROM native_monitoring_snapshots WHERE name = ? AND config_key = ?",
                                                {check->name, check->config_key});
            if (rows.empty())
            {
                auto empty = observation("unchecked", "not_checked");
                empty.update(Json{{"name", check->name}, {"kind", check->kind}, {"checked_at", nullptr}, {"changed_at", nullptr}, {"revision", 0}});
                snapshots.push_back(render(empty, std::nullopt, now, *check));
            }
            else
            {
                snapshots.push_back(render(cached_document(rows.front(), *check), cached_time(rows.front(), "checked_at"), now, *check));
            }
        }
        return {{"checks", std::move(snapshots)}, {"poll_interval_seconds", settings_.poll_interval}, {"snapshot_stale_after_seconds", settings_.stale_after}};
    }

    Json check(const Json &arguments)
    {
        const auto *target = select(arguments, true).front();
        const auto saved = run(*target);
        return {{"check", render(saved.snapshot, saved.checked_at, checked_now(context_), *target)},
                {"changed", saved.changed},
                {"events_queued", saved.events_queued}};
    }

    void poll()
    {
        bool failed = false;
        for (const auto &target : settings_.checks)
        {
            try
            {
                static_cast<void>(run(target));
            }
            catch (...)
            {
                failed = true;
            }
        }
        if (failed)
        {
            throw Error("monitoring_poll", "monitoring: some observations could not be recorded; all configured checks were attempted");
        }
    }

    Json name_schema() const
    {
        auto names = Json::array();
        for (const auto &check : settings_.checks)
        {
            names.push_back(check.name);
        }
        return {{"type", "string"}, {"minLength", 1}, {"maxLength", 64}, {"enum", std::move(names)}};
    }

    std::int64_t interval() const
    {
        return settings_.poll_interval;
    }

private:
    Context &context_;
    Settings settings_;

    std::vector<const Check *> select(const Json &arguments, const bool required) const
    {
        check_object(arguments, {"name"}, required ? std::vector<std::string>{"name"} : std::vector<std::string>{});
        std::vector<const Check *> selected;
        if (!arguments.contains("name"))
        {
            for (const auto &check : settings_.checks)
            {
                selected.push_back(&check);
            }
            return selected;
        }
        if (arguments.at("name").is_string())
        {
            for (const auto &check : settings_.checks)
            {
                if (arguments.at("name") == check.name)
                {
                    selected.push_back(&check);
                    return selected;
                }
            }
        }
        throw Error("monitoring_name", "monitoring: choose a configured check name");
    }

    static Json cached_document(const Json &row, const Check &check)
    {
        try
        {
            const auto snapshot = object_document(row.at("snapshot_json").get<std::string>(), REPORT_BYTES);
            check_object(snapshot, {"name", "kind", "status", "error", "reachable", "ready", "measurements", "report", "checked_at", "changed_at", "revision"},
                         {"name", "kind", "status", "error", "reachable", "ready", "measurements", "report", "checked_at", "changed_at", "revision"});
            static const std::set<std::string> statuses{"healthy", "reachable", "unhealthy", "stale", "error"};
            static const std::set<std::string> errors{"secret_unavailable",      "secret_invalid",        "endpoint_unreachable",   "endpoint_response_invalid",
                                                      "response_too_large",      "redirect_refused",      "unexpected_http_status", "response_malformed",
                                                      "readiness_field_missing", "readiness_mismatch",    "report_unavailable",     "report_malformed",
                                                      "report_schema_invalid",   "report_check_mismatch", "report_time_invalid",    "backup_report_stale",
                                                      "backup_failed",           "disk_unavailable",      "disk_data_invalid",      "disk_below_threshold",
                                                      "clock_moved_backwards",   "check_failed"};
            if (!snapshot.at("status").is_string() || !statuses.contains(snapshot.at("status").get<std::string>()) || snapshot.at("name") != check.name ||
                snapshot.at("kind") != check.kind || !snapshot.at("report").is_object() || !snapshot.at("measurements").is_object() ||
                !integer(snapshot.at("revision"), 1, MAX_JSON_INTEGER) ||
                (!snapshot.at("error").is_null() && (!snapshot.at("error").is_string() || !errors.contains(snapshot.at("error").get<std::string>()))) ||
                (!snapshot.at("reachable").is_null() && !snapshot.at("reachable").is_boolean()) ||
                (!snapshot.at("ready").is_null() && !snapshot.at("ready").is_boolean()))
            {
                throw Error("monitoring_cache", "monitoring: invalid cached observation");
            }
            const auto checked_at = cached_time(row, "checked_at");
            const auto changed_at = parse_timestamp(snapshot.at("changed_at").get<std::string>());
            if (parse_timestamp(snapshot.at("checked_at").get<std::string>()) != checked_at || changed_at > checked_at ||
                (row.contains("changed_at") && cached_time(row, "changed_at") != changed_at) ||
                (row.contains("revision") && row.at("revision") != snapshot.at("revision")) ||
                (snapshot.at("status") != Json("error") && snapshot.at("measurements").empty()) ||
                ((snapshot.at("status") == Json("healthy") || snapshot.at("status") == Json("reachable")) != snapshot.at("error").is_null()))
            {
                throw Error("monitoring_cache", "monitoring: inconsistent cached observation");
            }
            check_object(snapshot.at("measurements"), {"http_status", "response_bytes", "age_seconds", "duration_seconds", "max_age_seconds", "bytes_written",
                                                       "total_bytes", "used_bytes", "free_bytes", "free_percent", "min_free_bytes", "min_free_percent"});
            for (const auto &measurement : snapshot.at("measurements"))
            {
                if (!measurement.is_number() || !std::isfinite(measurement.get<double>()) || measurement.get<double>() < 0 ||
                    measurement.get<double>() > static_cast<double>(MAX_JSON_INTEGER))
                {
                    throw Error("monitoring_cache", "monitoring: invalid cached measurement");
                }
            }
            const auto &report = snapshot.at("report");
            if (!report.empty())
            {
                check_object(report, {"status", "started_at", "finished_at"}, {"status", "started_at", "finished_at"});
                const auto finished = report_time(report.at("finished_at"));
                if (check.kind != "backup" || (report.at("status") != Json("succeeded") && report.at("status") != Json("failed")) ||
                    report_time(report.at("started_at")) > finished || finished > checked_at * MICROSECONDS)
                {
                    throw Error("monitoring_cache", "monitoring: invalid cached report");
                }
            }
            else if (check.kind == "backup" && snapshot.at("status") != Json("error"))
            {
                throw Error("monitoring_cache", "monitoring: missing cached completion evidence");
            }
            if ((check.kind == "endpoint" && snapshot.at("status") == Json("healthy") &&
                 (snapshot.at("reachable") != true || snapshot.at("ready") != true || !check.settings.contains("readiness_field"))) ||
                (check.kind == "backup" && snapshot.at("status") == Json("healthy") && report.at("status") != Json("succeeded")) ||
                (check.kind != "endpoint" && (!snapshot.at("reachable").is_null() || !snapshot.at("ready").is_null())))
            {
                throw Error("monitoring_cache", "monitoring: cached readiness or producer outcome is inconsistent");
            }
            return snapshot;
        }
        catch (...)
        {
            throw Error("monitoring_cache", "monitoring: cached observation is corrupt");
        }
    }

    static std::int64_t cached_time(const Json &row, const std::string &field)
    {
        if (!row.contains(field) || !integer(row.at(field), 0, LAST_SECOND))
        {
            throw Error("monitoring_cache", "monitoring: cached observation time is corrupt");
        }
        return row.at(field).get<std::int64_t>();
    }

    Json render(Json snapshot, const std::optional<std::int64_t> checked_at, const std::int64_t now, const Check &check) const
    {
        const bool expired = checked_at && (now < *checked_at || now - *checked_at > settings_.stale_after);
        snapshot["observed_status"] = snapshot.at("status");
        snapshot["observed_error"] = snapshot.at("error");
        snapshot["snapshot_stale"] = expired;
        snapshot["report_stale"] = false;
        if (check.kind == "backup" && snapshot.at("report").contains("finished_at"))
        {
            std::int64_t finished = 0;
            try
            {
                finished = report_time(snapshot.at("report").at("finished_at"));
            }
            catch (...)
            {
                throw Error("monitoring_cache", "monitoring: cached report time is corrupt");
            }
            if (finished > now * MICROSECONDS)
            {
                snapshot["status"] = "stale";
                snapshot["error"] = "report_time_invalid";
            }
            else if (now * MICROSECONDS - finished > check.settings.at("max_age_seconds").get<std::int64_t>() * MICROSECONDS)
            {
                snapshot["report_stale"] = true;
                snapshot["status"] = "stale";
                snapshot["error"] = "backup_report_stale";
            }
        }
        if (expired)
        {
            snapshot["status"] = "stale";
            snapshot["error"] = "snapshot_stale";
        }
        return snapshot;
    }

    Saved run(const Check &check)
    {
        const auto began = checked_now(context_);
        Json measured;
        std::optional<std::string> report;
        try
        {
            if (check.kind == "endpoint")
            {
                measured = endpoint_observation(context_, check);
            }
            else if (check.kind == "backup")
            {
                report =
                    read_private_file(check.settings.at("directory").get<std::string>(), check.settings.at("filename").get<std::string>(), REPORT_BYTES, true);
            }
            else
            {
                std::error_code error;
                const auto space = std::filesystem::space(check.settings.at("path").get<std::string>(), error);
                if (error)
                {
                    measured = observation("error", "disk_unavailable");
                }
                else
                {
                    std::optional<std::int64_t> bytes;
                    std::optional<double> percent;
                    if (check.settings.contains("min_free_bytes"))
                    {
                        bytes = check.settings.at("min_free_bytes").get<std::int64_t>();
                    }
                    if (check.settings.contains("min_free_percent"))
                    {
                        percent = check.settings.at("min_free_percent").get<double>();
                    }
                    measured = monitoring_detail::disk_capacity(space.capacity, space.free, space.available, bytes, percent);
                }
            }
        }
        catch (...)
        {
            measured = observation("error", check.kind == "backup" ? "report_unavailable" : "check_failed");
        }
        const auto checked_at = checked_now(context_);
        if (report)
        {
            measured = backup_observation(check, *report, checked_at);
        }
        if (checked_at < began)
        {
            measured = observation("error", "clock_moved_backwards");
        }
        return record(check, measured, checked_at);
    }

    Saved record(const Check &check, const Json &measured, const std::int64_t checked_at)
    {
        const auto state_key = hash(Json{{"kind", check.kind},
                                         {"status", measured.at("status")},
                                         {"error", measured.at("error")},
                                         {"reachable", measured.at("reachable")},
                                         {"ready", measured.at("ready")},
                                         {"report_status", measured.at("report").value("status", Json(nullptr))}});
        Transaction transaction(context_.db);
        const auto rows = context_.db.query("SELECT config_key, state_key, snapshot_json, checked_at, changed_at, revision "
                                            "FROM native_monitoring_snapshots WHERE name = ?",
                                            {check.name});
        const Json *previous = rows.empty() ? nullptr : &rows.front();
        const bool same_config = previous && previous->at("config_key") == check.config_key;
        if (same_config && cached_time(*previous, "checked_at") > checked_at)
        {
            const auto snapshot = cached_document(*previous, check);
            const auto previous_time = cached_time(*previous, "checked_at");
            transaction.commit();
            return {snapshot, previous_time, false, 0};
        }
        const bool changed = !same_config || previous->at("state_key") != state_key;
        if (previous && !integer(previous->at("revision"), 1, MAX_JSON_INTEGER - (changed ? 1 : 0)))
        {
            throw Error("monitoring_cache", "monitoring: invalid or exhausted transition revision");
        }
        const std::int64_t revision = (previous ? previous->at("revision").get<std::int64_t>() : 0) + (changed ? 1 : 0);
        const auto changed_at = changed ? checked_at : cached_time(*previous, "changed_at");
        auto snapshot = measured;
        snapshot.update(Json{
            {"name", check.name}, {"kind", check.kind}, {"checked_at", timestamp(checked_at)}, {"changed_at", timestamp(changed_at)}, {"revision", revision}});
        const auto serialized = snapshot.dump();
        if (serialized.size() > REPORT_BYTES)
        {
            throw Error("monitoring_cache", "monitoring: observation exceeds storage bound");
        }
        context_.db.execute("INSERT INTO native_monitoring_snapshots "
                            "(name, config_key, state_key, snapshot_json, checked_at, changed_at, revision) VALUES (?, ?, ?, ?, ?, ?, ?) "
                            "ON CONFLICT(name) DO UPDATE SET config_key = excluded.config_key, state_key = excluded.state_key, "
                            "snapshot_json = excluded.snapshot_json, checked_at = excluded.checked_at, "
                            "changed_at = excluded.changed_at, revision = excluded.revision",
                            {check.name, check.config_key, state_key, serialized, checked_at, changed_at, revision});
        std::int64_t queued = 0;
        if (changed)
        {
            auto payload = snapshot;
            payload["previous_status"] = same_config ? cached_document(*previous, check).at("status") : Json(nullptr);
            if (payload.dump().size() > REPORT_BYTES)
            {
                throw Error("monitoring_cache", "monitoring: transition exceeds the event payload bound");
            }
            for (const auto *type : {"monitoring.changed", "monitoring.alert"})
            {
                if (std::string_view(type) == "monitoring.alert" && measured.at("status") != Json("error") && measured.at("status") != Json("unhealthy") &&
                    measured.at("status") != Json("stale"))
                {
                    continue;
                }
                const auto dedupe = "native:monitoring:" + check.name + ":" + std::to_string(revision) + ":" + type;
                context_.emit("monitoring", type, payload, dedupe);
                ++queued;
            }
        }
        transaction.commit();
        return {std::move(snapshot), checked_at, changed, queued};
    }
};
}  // namespace

void register_monitoring(Context &context, Registry &registry)
{
    if (!context.enabled("monitoring"))
    {
        return;
    }
    auto service = std::make_shared<Monitoring>(context, load_settings(context.settings("monitoring")));
    const auto properties = Json{{"name", service->name_schema()}};
    registry.add({"monitoring_status",
                  "Read configured cached observations only; no endpoint, report, secret, or capacity access.",
                  Tier::read,
                  object_schema(properties),
                  [service](const Json &arguments)
                  {
                      return service->status(arguments);
                  },
                  {}});
    registry.add({"monitoring_check",
                  "Run one named read-only observation; producer completion is not restore verification.",
                  Tier::read,
                  object_schema(properties, {"name"}),
                  [service](const Json &arguments)
                  {
                      return service->check(arguments);
                  },
                  {}});
    context.add_job("monitoring.poll", service->interval(),
                    [service]()
                    {
                        service->poll();
                    });
    auto &job = context.jobs.back();
    job.next_run = std::max(job.next_run, checked_now(context) + service->interval());
}
}  // namespace switchboard

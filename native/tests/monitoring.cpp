#include "support.hpp"

#include <array>
#include <cmath>
#include <fstream>
#include <limits>

namespace switchboard::monitoring_detail
{
Json disk_capacity(std::uintmax_t capacity, std::uintmax_t free, std::uintmax_t available, std::optional<std::int64_t> min_bytes,
                   std::optional<double> min_percent);
}

namespace
{
using namespace switchboard;
using switchboard::test::expect;
using switchboard::test::FakeHttp;

constexpr std::int64_t BASE_TIME = 1700000000;
const std::string ENDPOINT = "https://service.example/ready";

// Each fixture is a private directory under the test's working directory, removed on destruction.
class Workspace
{
public:
    Workspace()
    {
        static std::uint64_t sequence = 0;
        path = std::filesystem::current_path() / (".native-monitoring-test-" + std::to_string(getpid()) + "-" + std::to_string(++sequence));
        if (mkdir(path.c_str(), 0700) != 0)
        {
            throw std::runtime_error("cannot create private synthetic monitoring fixture");
        }
    }

    ~Workspace()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: synthetic monitoring fixture cleanup failed\n";
        }
    }

    Workspace(const Workspace &) = delete;
    Workspace &operator=(const Workspace &) = delete;

    void write(const std::string &filename, const std::string &body) const
    {
        const auto staging = path / ".producer-staging";
        {
            std::ofstream output(staging, std::ios::binary | std::ios::trunc);
            output.write(body.data(), static_cast<std::streamsize>(body.size()));
            output.close();
            expect(static_cast<bool>(output), "synthetic producer write must succeed");
        }
        expect(chmod(staging.c_str(), 0600) == 0, "synthetic reports must be private");
        std::filesystem::rename(staging, path / filename);
    }

    std::filesystem::path path;
};

struct Fixture
{
    Workspace directory;
    Database database{directory.path / "native-test.db"};
    FakeHttp http;
    std::int64_t now = BASE_TIME;

    std::int64_t count(const std::string &table)
    {
        expect(table == "native_events" || table == "native_monitoring_snapshots", "test table must be fixed");
        return database.query("SELECT COUNT(*) AS count FROM " + table).front().at("count").get<std::int64_t>();
    }

    std::vector<Json> events()
    {
        return database.query("SELECT source, type, payload, dedupe_key FROM native_events WHERE source = ? ORDER BY id", {"monitoring"});
    }

    Json row(const std::string &name = "server")
    {
        const auto rows = database.query("SELECT * FROM native_monitoring_snapshots WHERE name = ?", {name});
        expect(rows.size() == 1, "one configured observation must exist");
        return rows.front();
    }
};

Json endpoint()
{
    return {{"kind", "endpoint"}, {"url", ENDPOINT}, {"readiness_field", "ready"}, {"readiness_equals", true}};
}

Json options(const std::string &name = "server", Json check = endpoint())
{
    return {{"checks", {{name, std::move(check)}}}};
}

Json backup(const Fixture &fixture, const std::int64_t maximum_age = 30)
{
    return {{"kind", "backup"}, {"directory", fixture.directory.path.string()}, {"filename", "report.json"}, {"max_age_seconds", maximum_age}};
}

Json disk(const Fixture &fixture)
{
    return {{"kind", "disk"}, {"path", (fixture.directory.path / "uncreated-disk-path").string()}, {"min_free_bytes", 1}};
}

Json completion(const std::string &name = "backup", const std::int64_t finished = BASE_TIME - 30)
{
    return {{"schema_version", 1},  {"check", name}, {"status", "succeeded"}, {"started_at", timestamp(finished - 60)}, {"finished_at", timestamp(finished)},
            {"bytes_written", 4096}};
}

struct Session
{
    Json configuration;
    Context context;
    Registry registry;

    Session(Fixture &fixture, Json settings = options(), const bool enabled = true)
        : configuration{{"switchboard", {{"connectors", enabled ? Json::array({"monitoring"}) : Json::array()}, {"data_dir", fixture.directory.path.string()}}},
                        {"connectors", {{"monitoring", std::move(settings)}}}},
          context(configuration, fixture.directory.path, fixture.database, fixture.http,
                  [&fixture]()
                  {
                      return fixture.now;
                  })
    {
        register_monitoring(context, registry);
    }

    Json call(const std::string &tool, const Json &arguments)
    {
        const auto *selected = registry.find(tool);
        expect(selected != nullptr, "requested monitoring tool must be registered");
        return selected->handler(arguments);
    }

    Json check(const std::string &name = "server")
    {
        return call("monitoring_check", {{"name", name}});
    }

    Json status(const std::string &name = "server")
    {
        return call("monitoring_status", {{"name", name}}).at("checks").at(0);
    }
};

template <typename Function> void throws_error(Function function, const std::string &code = {})
{
    bool caught = false;
    try
    {
        function();
    }
    catch (const Error &error)
    {
        caught = true;
        expect(code.empty() || error.code() == code, "failure must use the expected safe error code");
    }
    expect(caught, "operation must fail closed");
}

void disabled_registration()
{
    Fixture fixture;
    Session session(fixture, {{"unsupported", true}}, false);
    expect(session.registry.tools().empty(), "disabled monitoring must register no tools");
    expect(session.context.jobs.empty(), "disabled monitoring must schedule no job");
    expect(fixture.http.requests.empty(), "disabled registration must be offline");
    expect(fixture.database.query("SELECT name FROM sqlite_master WHERE name = ?", {"native_monitoring_snapshots"}).empty(),
           "disabled registration must not create monitoring state");
}

void offline_registration_and_status()
{
    Fixture fixture;
    auto remote = endpoint();
    remote["bearer_token"] = "file:" + (fixture.directory.path / "uncreated-secret").string();
    auto report = backup(fixture);
    report["directory"] = (fixture.directory.path / "uncreated-reports").string();
    Session session(fixture, {{"checks", {{"server", remote}, {"backup", report}, {"capacity", disk(fixture)}}}});
    expect(session.registry.tools().size() == 2, "only the two read-only monitoring tools are exposed");
    for (const auto &[name, tool] : session.registry.tools())
    {
        static_cast<void>(name);
        expect(tool.tier == Tier::read, "monitoring tools must be read tier");
    }
    expect(session.context.jobs.size() == 1, "one bounded polling job is registered");
    const auto &job = session.context.jobs.front();
    expect(job.name == "monitoring.poll" && job.interval_seconds == 300, "poll job must use configured interval");
    expect(job.next_run >= fixture.now + 30, "registration must schedule a future check, not run it");
    const auto result = session.call("monitoring_status", Json::object());
    expect(result.at("checks").size() == 3, "cached status must include only selected named checks");
    expect(result.at("snapshot_stale_after_seconds") == 600, "default stale lifetime is explicit");
    for (const auto &check : result.at("checks"))
    {
        expect(check.at("status") == "unchecked" && check.at("error") == "not_checked", "missing cache is never healthy");
        expect(check.at("checked_at").is_null() && check.at("revision") == 0, "unchecked observations have no invented time");
    }
    expect(fixture.http.requests.empty(), "registration and status cannot request an endpoint");
    expect(fixture.count("native_monitoring_snapshots") == 0 && fixture.count("native_events") == 0, "cached reads cannot create observations or events");
    expect(!std::filesystem::exists(fixture.directory.path / "uncreated-reports"), "registration must not create report roots");
    expect(!std::filesystem::exists(fixture.directory.path / "uncreated-secret"), "registration must not resolve secrets");
}

void strict_top_level_settings()
{
    Fixture fixture;
    std::vector<Json> invalid{Json::array(),
                              Json::object(),
                              {{"checks", Json::object()}},
                              {{"checks", Json::array()}},
                              {{"checks", {{"BadName", endpoint()}}}},
                              {{"checks", {{"server", nullptr}}}}};
    for (const auto &key : {"enabled", "retry_seconds", "discovery", "commands", "notifications"})
    {
        auto value = options();
        value[key] = true;
        invalid.push_back(value);
    }
    for (const auto &value : {Json(true), Json(29), Json(86401), Json(30.0), Json(nullptr)})
    {
        auto settings = options();
        settings["poll_interval_seconds"] = value;
        invalid.push_back(settings);
    }
    for (const auto &value : {Json(false), Json(299), Json(604801), Json(600.0), Json(nullptr)})
    {
        auto settings = options();
        settings["snapshot_stale_after_seconds"] = value;
        invalid.push_back(settings);
    }
    auto excessive = Json::object();
    for (int index = 0; index < 33; ++index)
    {
        excessive["check_" + std::to_string(index)] = endpoint();
    }
    invalid.push_back({{"checks", excessive}});
    for (const auto &settings : invalid)
    {
        throws_error(
            [&fixture, &settings]()
            {
                Session session(fixture, settings);
            });
    }
    expect(fixture.http.requests.empty(), "invalid configuration cannot cause requests");
    expect(fixture.database.query("SELECT name FROM sqlite_master WHERE name = ?", {"native_monitoring_snapshots"}).empty(),
           "all configuration must validate before monitoring state is created");
}

void strict_endpoint_settings()
{
    Fixture fixture;
    std::vector<Json> invalid;
    const Json invalid_fields{{"method", "POST"},       {"headers", Json::object()}, {"retry_seconds", 1},   {"follow_redirects", true},
                              {"expected_status", 300}, {"allow_http", 1},           {"timeout_seconds", 31}};
    for (const auto &[key, value] : invalid_fields.items())
    {
        auto check = endpoint();
        check[key] = value;
        invalid.push_back(check);
    }
    for (const auto &url :
         {"http://localhost/ready", "http://169.254.169.254/ready", "http://service.example/ready", "https://user:password@service.example/ready",
          "https://service.example/ready?token=secret", "https://service.example/#fragment", "https://service.example/%2f", "https://service.example/../ready",
          "https://service.example:0/ready", "https://service.example:65536/ready", "https://service.example:080/ready", "https://0.0.0.0/ready",
          "https://[::]/ready", "https://224.0.0.1/ready", "https://999.1.1.1/ready", "https://service.example\\other/ready"})
    {
        auto check = endpoint();
        check["url"] = url;
        invalid.push_back(check);
        if (std::string(url).starts_with("http://") && std::string(url) != "http://localhost/ready")
        {
            check["allow_http"] = true;
            invalid.push_back(check);
        }
    }
    auto unpaired = endpoint();
    unpaired.erase("readiness_equals");
    invalid.push_back(unpaired);
    unpaired = endpoint();
    unpaired.erase("readiness_field");
    invalid.push_back(unpaired);
    for (const auto &value : {Json(nullptr), Json::array(), Json::object(), Json(1.0), Json(1000000001), Json(""), Json(std::string(129, 'x')),
                              Json("line\nbreak"), Json(std::numeric_limits<double>::infinity())})
    {
        auto check = endpoint();
        check["readiness_equals"] = value;
        invalid.push_back(check);
    }
    for (const auto &field : {"", ".ready", "items.0.ready", "ready..now", "a.b.c.d.e.f.g.h.i"})
    {
        auto check = endpoint();
        check["readiness_field"] = field;
        invalid.push_back(check);
    }
    for (const auto &reference : {"literal-token", "cmd:echo token", "file:relative/token", "env:BAD-NAME"})
    {
        auto check = endpoint();
        check["bearer_token"] = reference;
        invalid.push_back(check);
    }
    for (const auto &check : invalid)
    {
        throws_error(
            [&fixture, &check]()
            {
                Session session(fixture, options("server", check));
            });
    }
    expect(fixture.http.requests.empty(), "endpoint validation must remain lexical and offline");
}

void strict_backup_and_disk_settings()
{
    Fixture fixture;
    std::vector<Json> invalid;
    for (const auto &path : {"relative", "~/reports", "//reports", "/reports/../private", "/reports/./private", "/reports\\private"})
    {
        auto check = backup(fixture);
        check["directory"] = path;
        invalid.push_back(check);
    }
    for (const auto &filename : {"../report.json", "/report.json", "report.txt", ".report.json", "report.json/child"})
    {
        auto check = backup(fixture);
        check["filename"] = filename;
        invalid.push_back(check);
    }
    for (const auto &age : {Json(true), Json(29), Json(31536001), Json(30.0)})
    {
        auto check = backup(fixture);
        check["max_age_seconds"] = age;
        invalid.push_back(check);
    }
    auto check = backup(fixture);
    check["restore_command"] = "not supported";
    invalid.push_back(check);
    check = disk(fixture);
    check.erase("min_free_bytes");
    invalid.push_back(check);
    for (const auto &bytes : {Json(true), Json(0), Json(1.0), Json(MAX_JSON_INTEGER + 1)})
    {
        check = disk(fixture);
        check["min_free_bytes"] = bytes;
        invalid.push_back(check);
    }
    for (const auto &percent : {Json(false), Json(0), Json(-1), Json(101), Json(std::numeric_limits<double>::quiet_NaN())})
    {
        check = disk(fixture);
        check["min_free_percent"] = percent;
        invalid.push_back(check);
    }
    check = disk(fixture);
    check["ssh_host"] = "not supported";
    invalid.push_back(check);
    for (const auto &value : invalid)
    {
        throws_error(
            [&fixture, &value]()
            {
                Session session(fixture, options("target", value));
            });
    }
}

void named_arguments_and_schemas()
{
    Fixture fixture;
    Session session(fixture);
    for (const auto &name : {"monitoring_status", "monitoring_check"})
    {
        const auto *tool = session.registry.find(name);
        expect(tool->schema.at("additionalProperties") == false, "tool schema must refuse arbitrary operation inputs");
        expect(tool->schema.at("properties").at("name").at("enum") == Json::array({"server"}), "schema lists only configured names");
        throws_error(
            [&session, name]()
            {
                session.call(name, {{"name", "https://another.example/"}});
            });
        throws_error(
            [&session, name]()
            {
                session.call(name, {{"name", "server"}, {"url", "https://another.example/"}});
            });
        throws_error(
            [&session, name]()
            {
                session.call(name, {{"name", nullptr}});
            });
    }
    throws_error(
        [&session]()
        {
            session.call("monitoring_check", Json::object());
        });
    expect(fixture.http.requests.empty(), "bad tool arguments cannot trigger measurements");
}

void reachability_is_not_readiness()
{
    Fixture fixture;
    auto check = endpoint();
    check.erase("readiness_field");
    check.erase("readiness_equals");
    Session session(fixture, options("server", check));
    fixture.http.routes.push_back({"GET", ENDPOINT, {200, {}, "not a readiness document"}});
    const auto result = session.check();
    expect(result.at("check").at("status") == "reachable", "status-only evidence cannot certify readiness");
    expect(result.at("check").at("reachable") == true && result.at("check").at("ready").is_null(), "readiness remains unknown");
    expect(result.at("events_queued") == 1, "initial reachability is a change but not an alert");
}

void finite_typed_readiness_equality()
{
    Fixture fixture;
    Session session(fixture);
    for (const auto &value : {Json(1), Json(1.0), Json("true"), Json(nullptr), Json::array({true}), Json::object()})
    {
        fixture.http.add("GET", ENDPOINT, {{"ready", value}});
        const auto result = session.check().at("check");
        expect(result.at("status") == "unhealthy" && result.at("error") == "readiness_mismatch",
               "boolean readiness cannot coerce strings, numbers, containers, or null");
    }
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    expect(session.check().at("check").at("ready") == true, "typed boolean equality establishes configured readiness only");
    auto integer_check = endpoint();
    integer_check["readiness_equals"] = 1;
    Session integers(fixture, options("server", integer_check));
    fixture.http.add("GET", ENDPOINT, {{"ready", 1.0}});
    expect(integers.check().at("check").at("error") == "readiness_mismatch", "integer readiness rejects floating equality");
    fixture.http.add("GET", ENDPOINT, {{"ready", 1}});
    expect(integers.check().at("check").at("status") == "healthy", "integer equality accepts a finite integer");
}

void dotted_readiness_fields()
{
    Fixture fixture;
    auto check = endpoint();
    check["readiness_field"] = "service.state";
    check["readiness_equals"] = "ready";
    Session session(fixture, options("server", check));
    fixture.http.add("GET", ENDPOINT, {{"service", {{"state", "ready"}}}});
    expect(session.check().at("check").at("status") == "healthy", "dotted object-property equality is supported");
    for (const auto &document : {Json{{"service", true}}, Json{{"service", Json::array()}}, Json{{"other", "ready"}}})
    {
        fixture.http.add("GET", ENDPOINT, document);
        expect(session.check().at("check").at("error") == "readiness_field_missing", "missing or non-object traversal fails explicitly");
    }
}

void fixed_get_limits_and_http_consent()
{
    Fixture fixture;
    auto check = endpoint();
    check["url"] = "http://127.0.0.1:8080/ready";
    check["allow_http"] = true;
    check["timeout_seconds"] = 7;
    Session session(fixture, options("server", check));
    fixture.http.add("GET", check.at("url"), {{"ready", true}});
    session.check();
    const auto &request = fixture.http.requests.front();
    expect(request.method == "GET" && request.body.empty(), "monitoring must use a bodyless fixed-address read");
    expect(check.at("url") == Json(request.url) && request.allow_http, "only operator-consented plain transport is used");
    expect(request.timeout_seconds == 7 && request.max_bytes == 65536, "request timeout and response size are bounded");
    expect(request.headers.at("Accept") == "application/json" && !request.digest_password, "no optional authentication modes are invented");
}

void unexpected_status_redirect_and_oversize()
{
    Fixture fixture;
    Session session(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}}, 503);
    auto check = session.check().at("check");
    expect(check.at("status") == "unhealthy" && check.at("reachable") == true && check.at("ready") == false,
           "an error response proves reachability, not readiness");
    fixture.http.routes.push_back({"GET", ENDPOINT, {302, {{"Location", "https://unselected.example/"}}, "{}"}});
    check = session.check().at("check");
    expect(check.at("error") == "redirect_refused" && fixture.http.requests.size() == 2, "redirects cannot choose another destination");
    fixture.http.routes.push_back({"GET", ENDPOINT, {200, {}, std::string(65537, 'x')}});
    expect(session.check().at("check").at("error") == "response_too_large", "a nonconforming fake transport cannot bypass the cap");
    fixture.http.routes.push_back({"GET", ENDPOINT, {0, {}, "{}"}});
    expect(session.check().at("check").at("error") == "endpoint_response_invalid", "invalid transport status is not health");
}

void malformed_endpoint_documents()
{
    Fixture fixture;
    Session session(fixture);
    std::vector<std::string> bodies{"[]",
                                    "null",
                                    "{\"ready\":true,\"ready\":false}",
                                    "{\"ready\":NaN}",
                                    "{\"ready\":1e9999}",
                                    "{\"ready\":",
                                    std::string("{\"ready\":\"") + static_cast<char>(0xff) + "\"}"};
    bodies.push_back("{\"ready\":" + std::string(40, '[') + "true" + std::string(40, ']') + "}");
    for (const auto &body : bodies)
    {
        fixture.http.routes.push_back({"GET", ENDPOINT, {200, {}, body}});
        const auto check = session.check().at("check");
        expect(check.at("status") == "error" && check.at("error") == "response_malformed", "unsafe readiness documents fail closed");
    }
}

void lazy_private_secret_resolution()
{
    Fixture fixture;
    auto check = endpoint();
    const auto secret_path = fixture.directory.path / "token";
    check["bearer_token"] = "file:" + secret_path.string();
    Session session(fixture, options("server", check));
    expect(session.status().at("status") == "unchecked" && fixture.http.requests.empty(), "missing secrets cannot affect offline status");
    expect(session.check().at("check").at("error") == "secret_unavailable", "missing secrets are errors before any request");
    expect(fixture.http.requests.empty(), "unresolved credentials must not submit anonymous fallback requests");
    fixture.directory.write("token", "synthetic-monitoring-token");
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    expect(session.check().at("check").at("status") == "healthy", "a synthetic private file reference resolves only on check");
    expect(fixture.http.requests.back().headers.at("Authorization") == "Bearer synthetic-monitoring-token", "bearer authentication is explicit");
    std::filesystem::remove(secret_path);
    expect(session.status().at("status") == "healthy", "cached status does not re-resolve credentials");
}

void invalid_secrets_never_request()
{
    Fixture fixture;
    auto check = endpoint();
    check["bearer_token"] = "file:" + (fixture.directory.path / "token").string();
    Session session(fixture, options("server", check));
    for (const auto &token : {std::string(), std::string("token with spaces"), std::string(4097, 'x')})
    {
        fixture.directory.write("token", token);
        const auto result = session.check().at("check");
        expect(result.at("status") == "error", "empty, whitespace-bearing, or oversized credentials cannot become health");
        expect(result.at("error") == "secret_invalid" || result.at("error") == "secret_unavailable", "credential failure uses a safe code");
    }
    expect(fixture.http.requests.empty(), "invalid secrets cannot be sent");
}

void error_recovery_and_unchanged_transitions()
{
    Fixture fixture;
    Session session(fixture);
    auto result = session.check();
    expect(result.at("changed") == true && result.at("events_queued") == 2, "first transport error emits one change and one alert");
    const auto first = result.at("check");
    ++fixture.now;
    result = session.check();
    expect(result.at("changed") == false && result.at("events_queued") == 0, "unchanged errors do not flood events");
    expect(result.at("check").at("checked_at") != first.at("checked_at"), "unchanged checks still refresh observation time");
    expect(result.at("check").at("changed_at") == first.at("changed_at") && result.at("check").at("revision") == 1,
           "semantic revision and changed time do not follow measurement churn");
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    ++fixture.now;
    result = session.check();
    expect(result.at("check").at("revision") == 2 && result.at("events_queued") == 1, "recovery emits a change without an alert");
    fixture.http.add("GET", ENDPOINT, {{"ready", true}}, 500);
    ++fixture.now;
    result = session.check();
    expect(result.at("check").at("revision") == 3 && result.at("events_queued") == 2, "recurrence can alert again");
    const auto events = fixture.events();
    expect(events.size() == 5, "only semantic transitions emit events");
    expect(parse_json(events.at(2).at("payload")).at("previous_status") == "error", "recovery preserves prior observed status");
    expect(events.at(0).at("dedupe_key") != events.at(3).at("dedupe_key"), "revisions separate recurrence deduplication");
}

void endpoint_configuration_fingerprints()
{
    Fixture fixture;
    Session initial(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    initial.check();
    const auto original = fixture.row();
    const Json normalized{{"kind", "endpoint"},         {"url", ENDPOINT},         {"expected_status", 200}, {"timeout_seconds", 5}, {"allow_http", false},
                          {"readiness_field", "ready"}, {"readiness_equals", true}};
    expect(original.at("config_key") == hash(Json{{"native_monitoring_schema", 1}, {"name", "server"}, {"check", normalized}}),
           "configuration hash covers normalized check identity, defaults, and native schema");
    std::vector<Json> changed;
    const Json changes{{"url", "https://other.example/ready"},
                       {"expected_status", 204},
                       {"timeout_seconds", 6},
                       {"readiness_field", "other"},
                       {"readiness_equals", false},
                       {"allow_http", true},
                       {"bearer_token", "env:SWITCHBOARD_SYNTHETIC_UNRESOLVED"}};
    for (const auto &[key, value] : changes.items())
    {
        auto check = endpoint();
        check[key] = value;
        changed.push_back(check);
    }
    for (const auto &check : changed)
    {
        Session session(fixture, options("server", check));
        expect(session.status().at("status") == "unchecked", "changed endpoint criteria cannot reuse an old healthy snapshot");
    }
    expect(fixture.http.requests.size() == 1, "configuration invalidation must not measure or resolve secrets");
    auto different = endpoint();
    different["readiness_equals"] = false;
    Session replacement(fixture, options("server", different));
    fixture.http.add("GET", ENDPOINT, {{"ready", false}});
    const auto checked = replacement.check();
    expect(checked.at("check").at("revision") == 2 && checked.at("changed") == true, "configuration changes advance monotonic revision");
    expect(fixture.row().at("config_key") != original.at("config_key"), "new observations use their current configuration fingerprint");
    expect(parse_json(fixture.events().back().at("payload")).at("previous_status").is_null(), "unrelated targets have no inherited health");
}

void equivalent_defaults_reuse_configuration()
{
    Fixture fixture;
    Session initial(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    initial.check();
    auto explicit_defaults = endpoint();
    explicit_defaults.update(Json{{"expected_status", 200}, {"timeout_seconds", 5}, {"allow_http", false}});
    Session equivalent(fixture, options("server", explicit_defaults));
    expect(equivalent.status().at("status") == "healthy", "explicit defaults and omitted defaults have the same normalized meaning");
    expect(fixture.http.requests.size() == 1, "reusing equivalent cached data is offline");
}

void snapshot_stale_and_backward_clock_boundaries()
{
    Fixture fixture;
    Session session(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    session.check();
    const auto events = fixture.count("native_events");
    fixture.now = BASE_TIME + 600;
    expect(session.status().at("status") == "healthy", "snapshot lifetime equality remains current");
    ++fixture.now;
    auto status = session.status();
    expect(status.at("status") == "stale" && status.at("error") == "snapshot_stale", "expired cache is never current health");
    expect(status.at("observed_status") == "healthy" && status.at("snapshot_stale") == true, "old evidence is labeled honestly");
    fixture.now = BASE_TIME - 1;
    status = session.status();
    expect(status.at("status") == "stale" && status.at("snapshot_stale") == true, "backward time makes cached evidence stale");
    expect(fixture.count("native_events") == events && fixture.http.requests.size() == 1, "expiry rendering neither measures nor emits events");
}

void current_snapshot_lifetime_applies_to_cache()
{
    Fixture fixture;
    Session initial(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    initial.check();
    auto settings = options();
    settings["snapshot_stale_after_seconds"] = 300;
    Session shortened(fixture, settings);
    fixture.now = BASE_TIME + 301;
    expect(shortened.status().at("error") == "snapshot_stale", "a new lifetime is applied to old observations without measuring");
}

void older_observations_do_not_replace_newer()
{
    Fixture fixture;
    Session session(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    session.check();
    const auto saved = fixture.row();
    --fixture.now;
    fixture.http.add("GET", ENDPOINT, {{"ready", false}});
    const auto result = session.check();
    expect(result.at("changed") == false && result.at("events_queued") == 0, "older observations cannot generate misleading transitions");
    expect(fixture.row() == saved, "older measurements cannot overwrite a newer sample of the same target");
    expect(result.at("check").at("status") == "stale", "returning the newer cache under a backward clock is still labeled stale");
}

void invalid_clock_prevents_checks()
{
    Fixture fixture;
    Session session(fixture);
    for (const auto value : {std::int64_t(-1), std::int64_t(253402300800LL)})
    {
        fixture.now = value;
        throws_error(
            [&session]()
            {
                session.status();
            },
            "monitoring_clock");
        throws_error(
            [&session]()
            {
                session.check();
            },
            "monitoring_clock");
    }
    expect(fixture.http.requests.empty() && fixture.count("native_monitoring_snapshots") == 0, "invalid observation time cannot trigger a check");
}

void fresh_backup_boundary_and_cached_age()
{
    Fixture fixture;
    fixture.directory.write("report.json", completion().dump());
    Session session(fixture, options("backup", backup(fixture)));
    const auto result = session.check("backup");
    expect(result.at("check").at("status") == "healthy" && result.at("check").at("measurements").at("age_seconds") == 30,
           "backup completion freshness equality passes");
    const auto events = fixture.count("native_events");
    std::filesystem::remove(fixture.directory.path / "report.json");
    ++fixture.now;
    const auto cached = session.status("backup");
    expect(cached.at("status") == "stale" && cached.at("report_stale") == true && cached.at("snapshot_stale") == false,
           "completion evidence expires even before the cached snapshot lifetime");
    expect(cached.at("observed_status") == "healthy" && cached.at("measurements").at("age_seconds") == 30,
           "cached freshness does not invent a new measurement");
    expect(fixture.count("native_events") == events, "offline backup expiry does not insert events or reopen the deleted report");
}

void future_completion_precedes_freshness()
{
    Fixture fixture;
    Session session(fixture, options("backup", backup(fixture)));
    for (const auto &finished : {Json(timestamp(BASE_TIME + 1)), Json(timestamp(BASE_TIME).substr(0, 19) + ".000001Z")})
    {
        auto report = completion();
        report["finished_at"] = finished;
        fixture.directory.write("report.json", report.dump());
        const auto result = session.check("backup").at("check");
        expect(result.at("status") == "error" && result.at("error") == "report_time_invalid",
               "future completions cannot pass freshness through a negative age");
        expect(result.at("measurements").empty() && result.at("report").empty(), "invalid producer chronology is not success evidence");
    }
}

void fractional_freshness_boundaries()
{
    Fixture fixture;
    Session session(fixture, options("backup", backup(fixture)));
    auto report = completion();
    report["finished_at"] = timestamp(BASE_TIME - 30).substr(0, 19) + ".000001Z";
    fixture.directory.write("report.json", report.dump());
    expect(session.check("backup").at("check").at("status") == "healthy", "one microsecond inside the completion limit passes");
    report["finished_at"] = timestamp(BASE_TIME - 31).substr(0, 19) + ".999999Z";
    fixture.directory.write("report.json", report.dump());
    const auto stale = session.check("backup").at("check");
    expect(stale.at("status") == "stale" && stale.at("error") == "backup_report_stale", "one microsecond outside the limit is stale");
    expect(std::abs(stale.at("measurements").at("age_seconds").get<double>() - 30.000001) < 0.0000001,
           "report precision is preserved despite whole-second observation clocks");
}

void aware_timestamp_normalization_and_equal_chronology()
{
    Fixture fixture;
    auto report = completion();
    report["started_at"] = "2023-11-15T00:12:50+02:00";
    report["finished_at"] = "2023-11-14T20:12:50-02:00";
    fixture.directory.write("report.json", report.dump());
    Session session(fixture, options("backup", backup(fixture)));
    const auto result = session.check("backup").at("check");
    expect(result.at("status") == "healthy", "equivalent aware offsets and equal start/finish are valid");
    expect(result.at("report").at("finished_at") == "2023-11-14T22:12:50.000000Z", "stored producer times normalize to universal time");
    expect(result.at("measurements").at("duration_seconds") == 0, "equal start and completion has zero duration");
}

void invalid_backup_timestamps()
{
    Fixture fixture;
    Session session(fixture, options("backup", backup(fixture)));
    const std::vector<Json> times{"2023-11-14",
                                  "2023-11-14T22:12:50",
                                  "2023-11-14T22:12:50-00:00",
                                  "2023-02-30T22:12:50Z",
                                  "2023-11-14T24:12:50Z",
                                  "2023-11-14T22:12:60Z",
                                  "2023-11-14T22:12:50+24:00",
                                  "2023-11-14T22:12:50+00:60",
                                  "2023-11-14T22:12:50.0000001Z",
                                  "1969-12-31T23:59:59Z",
                                  "1970-01-01T00:00:00+01:00",
                                  "10000-01-01T00:00:00Z",
                                  true,
                                  1700000000,
                                  nullptr};
    for (const auto &value : times)
    {
        auto report = completion();
        report["finished_at"] = value;
        fixture.directory.write("report.json", report.dump());
        expect(session.check("backup").at("check").at("error") == "report_time_invalid", "invalid or naive producer times fail closed");
    }
    auto reversed = completion();
    reversed["started_at"] = timestamp(BASE_TIME - 1);
    fixture.directory.write("report.json", reversed.dump());
    expect(session.check("backup").at("check").at("error") == "report_time_invalid", "start cannot follow completion");
}

void strict_report_schema_and_identity()
{
    Fixture fixture;
    Session session(fixture, options("backup", backup(fixture)));
    std::vector<Json> invalid;
    const Json invalid_fields{{"schema_version", true}, {"check", false}, {"status", "running"}, {"output", "secret producer output"}, {"bytes_written", true}};
    for (const auto &[key, value] : invalid_fields.items())
    {
        auto report = completion();
        report[key] = value;
        invalid.push_back(report);
    }
    for (const auto &value : {Json(1.0), Json(2), Json(nullptr)})
    {
        auto report = completion();
        report["schema_version"] = value;
        invalid.push_back(report);
    }
    for (const auto &value : {Json(-1), Json(1.0), Json(MAX_JSON_INTEGER + 1)})
    {
        auto report = completion();
        report["bytes_written"] = value;
        invalid.push_back(report);
    }
    for (const auto &key : {"schema_version", "check", "status", "started_at", "finished_at"})
    {
        auto report = completion();
        report.erase(key);
        invalid.push_back(report);
    }
    for (const auto &report : invalid)
    {
        fixture.directory.write("report.json", report.dump());
        expect(session.check("backup").at("check").at("error") == "report_schema_invalid", "unsupported producer schemas cannot be healthy");
    }
    auto wrong_identity = completion("other_backup");
    fixture.directory.write("report.json", wrong_identity.dump());
    expect(session.check("backup").at("check").at("error") == "report_check_mismatch", "a completion must identify this exact configured check");
    auto minimal = completion();
    minimal.erase("bytes_written");
    fixture.directory.write("report.json", minimal.dump());
    expect(session.check("backup").at("check").at("status") == "healthy", "byte counts are optional, not silently fabricated");
}

void missing_malformed_report_and_recovery()
{
    Fixture fixture;
    Session session(fixture, options("backup", backup(fixture)));
    expect(session.check("backup").at("check").at("error") == "report_unavailable", "missing reports are errors");
    for (const auto &body : {"not json", "[]", "{\"schema_version\":1,\"schema_version\":1}", "{\"value\":NaN}"})
    {
        fixture.directory.write("report.json", body);
        expect(session.check("backup").at("check").at("error") == "report_malformed", "malformed and duplicate reports are not success");
    }
    fixture.directory.write("report.json", completion().dump());
    const auto result = session.check("backup");
    expect(result.at("check").at("status") == "healthy" && result.at("changed") == true, "valid subsequent evidence can recover");
    expect(fixture.http.requests.empty(), "report monitoring never contacts an endpoint");
}

void private_report_root_and_writable_file_refusal()
{
    Fixture fixture;
    fixture.directory.write("report.json", completion().dump());
    Session session(fixture, options("backup", backup(fixture)));
    expect(chmod(fixture.directory.path.c_str(), 0750) == 0, "synthetic shared root setup");
    expect(session.check("backup").at("check").at("error") == "report_unavailable", "a non-700 report root is refused read-only");
    expect(chmod(fixture.directory.path.c_str(), 0700) == 0, "synthetic private root restoration");
    for (const auto mode : {mode_t(0660), mode_t(0644)})
    {
        expect(chmod((fixture.directory.path / "report.json").c_str(), mode) == 0, "synthetic shared report setup");
        expect(session.check("backup").at("check").at("error") == "report_unavailable", "shared reports are refused even inside a private root");
    }
    expect(chmod((fixture.directory.path / "report.json").c_str(), 0600) == 0, "synthetic private file restoration");
    expect(session.check("backup").at("check").at("status") == "healthy", "protection repairs must be made by the operator, not monitoring");
}

void linked_and_nonregular_reports()
{
    Fixture fixture;
    fixture.directory.write("source.json", completion().dump());
    std::filesystem::create_symlink("source.json", fixture.directory.path / "report.json");
    Session session(fixture, options("backup", backup(fixture)));
    expect(session.check("backup").at("check").at("error") == "report_unavailable", "the report basename cannot be a symbolic link");
    std::filesystem::remove(fixture.directory.path / "report.json");
    expect(mkfifo((fixture.directory.path / "report.json").c_str(), 0600) == 0, "synthetic nonregular fixture setup");
    expect(session.check("backup").at("check").at("error") == "report_unavailable", "nonregular reports fail without waiting for a writer");
    std::filesystem::create_directory_symlink(fixture.directory.path, fixture.directory.path / "linked-root");
    auto check = backup(fixture);
    check["directory"] = (fixture.directory.path / "linked-root").string() + "/";
    Session linked(fixture, options("backup", check));
    expect(linked.check("backup").at("check").at("error") == "report_unavailable", "trailing separators cannot bypass root link refusal");
}

void report_size_boundaries()
{
    Fixture fixture;
    auto body = completion().dump();
    body.append(16384 - body.size(), ' ');
    fixture.directory.write("report.json", body);
    Session session(fixture, options("backup", backup(fixture)));
    expect(session.check("backup").at("check").at("status") == "healthy", "the exact private report byte cap is accepted");
    body.push_back(' ');
    fixture.directory.write("report.json", body);
    const auto result = session.check("backup").at("check");
    expect(result.at("status") == "error" && result.at("error") == "report_unavailable", "oversized private reads fail closed");
    expect(result.at("measurements").empty(), "file failures cannot become made-up measurements");
}

void failed_and_stale_report_transitions()
{
    Fixture fixture;
    auto report = completion();
    report["status"] = "failed";
    fixture.directory.write("report.json", report.dump());
    Session session(fixture, options("backup", backup(fixture)));
    auto result = session.check("backup");
    expect(result.at("check").at("error") == "backup_failed" && result.at("events_queued") == 2, "a fresh failed attempt alerts");
    ++fixture.now;
    result = session.check("backup");
    expect(result.at("check").at("error") == "backup_report_stale" && result.at("check").at("report").at("status") == "failed",
           "stale failed evidence preserves the producer's failed outcome");
    report["status"] = "succeeded";
    fixture.directory.write("report.json", report.dump());
    result = session.check("backup");
    expect(result.at("changed") == true && result.at("events_queued") == 2, "outcome changes are semantic even if both reports remain stale");
    result = session.check("backup");
    expect(result.at("changed") == false && result.at("events_queued") == 0, "unchanged stale reports do not flood alerts");
}

void backup_configuration_fingerprints()
{
    Fixture fixture;
    fixture.directory.write("report.json", completion().dump());
    Session initial(fixture, options("backup", backup(fixture)));
    initial.check("backup");
    std::vector<Json> changed;
    const Json changes{{"filename", "different.json"}, {"directory", (fixture.directory.path / "uncreated").string()}, {"max_age_seconds", 60}};
    for (const auto &[key, value] : changes.items())
    {
        auto check = backup(fixture);
        check[key] = value;
        changed.push_back(check);
    }
    for (const auto &check : changed)
    {
        Session session(fixture, options("backup", check));
        expect(session.status("backup").at("status") == "unchecked", "changed backup paths or freshness limits cannot reuse old healthy data");
    }
    Session different_kind(fixture, options("backup", disk(fixture)));
    expect(different_kind.status("backup").at("status") == "unchecked", "the same name under another kind cannot reuse evidence");
}

void disk_byte_thresholds_and_reserved_capacity()
{
    const auto equal = monitoring_detail::disk_capacity(100, 40, 25, 25, std::nullopt);
    expect(equal.at("status") == "healthy", "available-byte threshold equality passes");
    expect(equal.at("measurements").at("used_bytes") == 60 && equal.at("measurements").at("free_bytes") == 25,
           "reserved blocks are neither used nor available free space");
    expect(equal.at("measurements").at("free_percent") == 25, "percentage uses caller-available space over total capacity");
    const auto below = monitoring_detail::disk_capacity(100, 40, 25, 26, std::nullopt);
    expect(below.at("status") == "unhealthy" && below.at("error") == "disk_below_threshold", "one available byte below the threshold alerts");
    expect(monitoring_detail::disk_capacity(100, 40, 0, 1, std::nullopt).at("status") == "unhealthy",
           "valid zero available space is low capacity, not unavailable data");
}

void disk_percentage_and_either_threshold()
{
    expect(monitoring_detail::disk_capacity(100, 40, 25, std::nullopt, 25.0).at("status") == "healthy", "percentage equality passes");
    expect(monitoring_detail::disk_capacity(100, 40, 25, std::nullopt, 25.1).at("status") == "unhealthy", "percentage below minimum alerts");
    expect(monitoring_detail::disk_capacity(100, 40, 25, 26, 20.0).at("status") == "unhealthy", "bytes alone can violate paired minima");
    expect(monitoring_detail::disk_capacity(100, 40, 25, 20, 26.0).at("status") == "unhealthy", "percentage alone can violate paired minima");
}

void invalid_disk_data_is_not_zero_health()
{
    for (const auto &values : std::vector<std::array<std::uintmax_t, 3>>{{0, 0, 0},
                                                                         {100, 101, 20},
                                                                         {100, 40, 41},
                                                                         {static_cast<std::uintmax_t>(MAX_JSON_INTEGER) + 1, 0, 0},
                                                                         {std::numeric_limits<std::uintmax_t>::max(), 0, 0}})
    {
        const auto result = monitoring_detail::disk_capacity(values[0], values[1], values[2], 1, std::nullopt);
        expect(result.at("status") == "error" && result.at("error") == "disk_data_invalid", "invalid capacity cannot be treated as healthy");
        expect(result.at("measurements").empty(), "invalid data cannot synthesize zero usage");
    }
}

void missing_disk_path_is_an_error()
{
    Fixture fixture;
    Session session(fixture, options("capacity", disk(fixture)));
    expect(session.status("capacity").at("status") == "unchecked", "disk configuration and status do not measure capacity");
    const auto result = session.check("capacity").at("check");
    expect(result.at("status") == "error" && result.at("error") == "disk_unavailable", "a missing configured path is not a healthy fallback");
    expect(result.at("measurements").empty() && fixture.http.requests.empty(), "disk failures create no synthetic capacity or network request");
}

void disk_configuration_fingerprints()
{
    Fixture fixture;
    Session initial(fixture, options("capacity", disk(fixture)));
    initial.check("capacity");
    const Json changes{{"path", (fixture.directory.path / "different-uncreated-path").string()}, {"min_free_bytes", 2}, {"min_free_percent", 10}};
    for (const auto &[key, value] : changes.items())
    {
        auto check = disk(fixture);
        check[key] = value;
        Session changed(fixture, options("capacity", check));
        expect(changed.status("capacity").at("status") == "unchecked", "disk path and either threshold must affect cache identity");
    }
}

void atomic_event_failure_rolls_back_snapshot()
{
    Fixture fixture;
    Session session(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    session.check();
    const auto original = fixture.row();
    const auto event_count = fixture.count("native_events");
    fixture.database.execute("CREATE TRIGGER monitoring_fail_alert BEFORE INSERT ON native_events "
                             "WHEN NEW.source = 'monitoring' AND NEW.type = 'monitoring.alert' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic alert insertion failure'); END");
    ++fixture.now;
    fixture.http.add("GET", ENDPOINT, {{"ready", false}});
    throws_error(
        [&session]()
        {
            session.check();
        });
    expect(fixture.row() == original && fixture.count("native_events") == event_count,
           "a second event failure must roll back both the updated snapshot and first event");
    fixture.database.execute("DROP TRIGGER monitoring_fail_alert");
    fixture.http.add("GET", ENDPOINT, {{"ready", false}});
    const auto recovered = session.check();
    expect(recovered.at("check").at("revision") == 2 && recovered.at("events_queued") == 2,
           "a rolled-back transition consumes neither revision nor deduplication keys");
}

void atomic_first_observation_failure()
{
    Fixture fixture;
    Session session(fixture);
    fixture.database.execute("CREATE TRIGGER monitoring_fail_first_alert BEFORE INSERT ON native_events "
                             "WHEN NEW.source = 'monitoring' AND NEW.type = 'monitoring.alert' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic first alert failure'); END");
    throws_error(
        [&session]()
        {
            session.check();
        });
    expect(fixture.count("native_monitoring_snapshots") == 0 && fixture.count("native_events") == 0,
           "first-observation event failure cannot leave a partial snapshot or event");
    expect(session.status().at("status") == "unchecked", "failed recording must not be reported as observed health");
}

void poll_continues_after_storage_failure()
{
    Fixture fixture;
    auto first = endpoint();
    auto second = endpoint();
    second["url"] = "https://second.example/ready";
    Session session(fixture, {{"checks", {{"a", first}, {"b", second}}}, {"poll_interval_seconds", 30}});
    fixture.database.execute("CREATE TRIGGER monitoring_fail_a BEFORE INSERT ON native_events "
                             "WHEN NEW.dedupe_key LIKE 'native:monitoring:a:%' "
                             "BEGIN SELECT RAISE(ABORT, 'synthetic named recording failure'); END");
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    fixture.http.add("GET", second.at("url"), {{"ready", true}});
    throws_error(
        [&session]()
        {
            session.context.jobs.front().handler();
        },
        "monitoring_poll");
    expect(fixture.http.requests.size() == 2, "poll attempts later configured checks after a storage failure");
    expect(session.status("a").at("status") == "unchecked" && session.status("b").at("status") == "healthy", "poll reports only committed observations");
    expect(fixture.count("native_monitoring_snapshots") == 1 && fixture.count("native_events") == 1, "failed target does not leak partial state");
}

void poll_contains_measurement_failures()
{
    Fixture fixture;
    Session session(fixture, {{"checks", {{"a", backup(fixture)}, {"b", endpoint()}}}});
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    session.context.jobs.front().handler();
    expect(session.status("a").at("status") == "error" && session.status("b").at("status") == "healthy",
           "a missing report is an error observation, not a reason to skip other configured checks");
    expect(fixture.count("native_events") == 3, "poll emits bounded error and success transitions independently");
}

void corrupt_cache_fails_closed()
{
    Fixture fixture;
    Session session(fixture);
    fixture.http.add("GET", ENDPOINT, {{"ready", true}});
    session.check();
    const auto original = parse_json(fixture.row().at("snapshot_json"));
    std::vector<std::string> corrupt{"not json"};
    const Json corrupt_fields{{"error", "external exception text"}, {"kind", "disk"}, {"checked_at", timestamp(BASE_TIME + 1)}, {"ready", "true"}};
    for (const auto &[key, value] : corrupt_fields.items())
    {
        auto snapshot = original;
        snapshot[key] = value;
        corrupt.push_back(snapshot.dump());
    }
    for (const auto &serialized : corrupt)
    {
        fixture.database.execute("UPDATE native_monitoring_snapshots SET snapshot_json = ? WHERE name = ?", {serialized, "server"});
        throws_error(
            [&session]()
            {
                session.status();
            },
            "monitoring_cache");
    }
    expect(fixture.http.requests.size() == 1, "corrupt cache must not trigger a hidden refresh or invent healthy fallback");
}

void event_payloads_are_bounded_and_redacted()
{
    Fixture fixture;
    auto check = endpoint();
    check["readiness_equals"] = "synthetic-criterion";
    check["bearer_token"] = "file:" + (fixture.directory.path / "token").string();
    fixture.directory.write("token", "synthetic-private-token");
    Session session(fixture, options("server", check));
    fixture.http.add("GET", ENDPOINT, {{"ready", "synthetic-criterion"}, {"debug", "synthetic-secret-response-body"}});
    const auto output = session.check();
    const auto serialized = output.dump() + fixture.row().dump() + Json(fixture.events()).dump();
    for (const auto &forbidden : {"synthetic-private-token", "synthetic-criterion", "synthetic-secret-response-body", "Authorization", "file:"})
    {
        expect(serialized.find(forbidden) == std::string::npos, "raw secrets, criteria, and response fields cannot leave measurement code");
    }
    for (const auto &event : fixture.events())
    {
        const auto payload = parse_json(event.at("payload"));
        expect(payload.contains("checked_at") && payload.contains("error") && payload.contains("previous_status"),
               "events include observation time, safe failure state, and transition context");
        expect(event.at("payload").get_ref<const std::string &>().size() <= 16384, "event payload must remain bounded");
        expect(payload.dump().find(ENDPOINT) == std::string::npos && payload.dump().find(fixture.directory.path.string()) == std::string::npos,
               "events contain no configured addresses or paths");
    }
}
}  // namespace

int main()
{
    const std::vector<std::pair<const char *, void (*)()>> tests{
        {"disabled_registration", disabled_registration},
        {"offline_registration_and_status", offline_registration_and_status},
        {"strict_top_level_settings", strict_top_level_settings},
        {"strict_endpoint_settings", strict_endpoint_settings},
        {"strict_backup_and_disk_settings", strict_backup_and_disk_settings},
        {"named_arguments_and_schemas", named_arguments_and_schemas},
        {"reachability_is_not_readiness", reachability_is_not_readiness},
        {"finite_typed_readiness_equality", finite_typed_readiness_equality},
        {"dotted_readiness_fields", dotted_readiness_fields},
        {"fixed_get_limits_and_http_consent", fixed_get_limits_and_http_consent},
        {"unexpected_status_redirect_and_oversize", unexpected_status_redirect_and_oversize},
        {"malformed_endpoint_documents", malformed_endpoint_documents},
        {"lazy_private_secret_resolution", lazy_private_secret_resolution},
        {"invalid_secrets_never_request", invalid_secrets_never_request},
        {"error_recovery_and_unchanged_transitions", error_recovery_and_unchanged_transitions},
        {"endpoint_configuration_fingerprints", endpoint_configuration_fingerprints},
        {"equivalent_defaults_reuse_configuration", equivalent_defaults_reuse_configuration},
        {"snapshot_stale_and_backward_clock_boundaries", snapshot_stale_and_backward_clock_boundaries},
        {"current_snapshot_lifetime_applies_to_cache", current_snapshot_lifetime_applies_to_cache},
        {"older_observations_do_not_replace_newer", older_observations_do_not_replace_newer},
        {"invalid_clock_prevents_checks", invalid_clock_prevents_checks},
        {"fresh_backup_boundary_and_cached_age", fresh_backup_boundary_and_cached_age},
        {"future_completion_precedes_freshness", future_completion_precedes_freshness},
        {"fractional_freshness_boundaries", fractional_freshness_boundaries},
        {"aware_timestamp_normalization_and_equal_chronology", aware_timestamp_normalization_and_equal_chronology},
        {"invalid_backup_timestamps", invalid_backup_timestamps},
        {"strict_report_schema_and_identity", strict_report_schema_and_identity},
        {"missing_malformed_report_and_recovery", missing_malformed_report_and_recovery},
        {"private_report_root_and_writable_file_refusal", private_report_root_and_writable_file_refusal},
        {"linked_and_nonregular_reports", linked_and_nonregular_reports},
        {"report_size_boundaries", report_size_boundaries},
        {"failed_and_stale_report_transitions", failed_and_stale_report_transitions},
        {"backup_configuration_fingerprints", backup_configuration_fingerprints},
        {"disk_byte_thresholds_and_reserved_capacity", disk_byte_thresholds_and_reserved_capacity},
        {"disk_percentage_and_either_threshold", disk_percentage_and_either_threshold},
        {"invalid_disk_data_is_not_zero_health", invalid_disk_data_is_not_zero_health},
        {"missing_disk_path_is_an_error", missing_disk_path_is_an_error},
        {"disk_configuration_fingerprints", disk_configuration_fingerprints},
        {"atomic_event_failure_rolls_back_snapshot", atomic_event_failure_rolls_back_snapshot},
        {"atomic_first_observation_failure", atomic_first_observation_failure},
        {"poll_continues_after_storage_failure", poll_continues_after_storage_failure},
        {"poll_contains_measurement_failures", poll_contains_measurement_failures},
        {"corrupt_cache_fails_closed", corrupt_cache_fails_closed},
        {"event_payloads_are_bounded_and_redacted", event_payloads_are_bounded_and_redacted}};
    for (const auto &[name, test] : tests)
    {
        try
        {
            test();
        }
        catch (const std::exception &error)
        {
            std::cerr << "FAIL " << name << ": " << error.what() << "\n";
            return 1;
        }
    }
    std::cout << "PASS " << tests.size() << " native monitoring tests\n";
    return 0;
}

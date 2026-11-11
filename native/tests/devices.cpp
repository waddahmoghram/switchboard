#include "support.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace switchboard::test
{
namespace
{
const std::string WLED = "http://192.168.50.20";
const std::string SHELLY = "http://192.168.50.40";
const std::string SHELLY_ID = "shellyplusplugs-000000000001";
const std::string PASSWORD_VARIABLE = "SWITCHBOARD_NATIVE_DEVICES_AUTHORED_TEST_PASSWORD";
const std::string PASSWORD = "synthetic-device-password";

class ProjectDirectory
{
public:
    ProjectDirectory()
    {
        static std::atomic<unsigned int> sequence{0};
        const auto tick = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::current_path() /
               ("native-devices-authored-test-" + std::to_string(getpid()) + "-" + std::to_string(tick) + "-" + std::to_string(sequence++));
        if (!std::filesystem::create_directory(path))
        {
            throw std::runtime_error("cannot create a private project-local test directory");
        }
        if (chmod(path.c_str(), 0700) != 0)
        {
            std::error_code error;
            std::filesystem::remove(path, error);
            throw std::runtime_error("cannot protect the project-local test directory");
        }
    }

    ~ProjectDirectory()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: project-local device test cleanup failed\n";
        }
    }

    ProjectDirectory(const ProjectDirectory &) = delete;
    ProjectDirectory &operator=(const ProjectDirectory &) = delete;
    std::filesystem::path path;
};

class PasswordEnvironment
{
public:
    explicit PasswordEnvironment(const std::optional<std::string> &value = PASSWORD)
    {
        const auto *old = std::getenv(PASSWORD_VARIABLE.c_str());
        if (old != nullptr)
        {
            previous_ = old;
        }
        const auto result = value ? setenv(PASSWORD_VARIABLE.c_str(), value->c_str(), 1) : unsetenv(PASSWORD_VARIABLE.c_str());
        expect(result == 0, "cannot arrange synthetic test credential");
    }

    ~PasswordEnvironment()
    {
        const auto result = previous_ ? setenv(PASSWORD_VARIABLE.c_str(), previous_->c_str(), 1) : unsetenv(PASSWORD_VARIABLE.c_str());
        if (result != 0)
        {
            std::cerr << "ERROR: synthetic credential environment cleanup failed\n";
        }
    }

    PasswordEnvironment(const PasswordEnvironment &) = delete;
    PasswordEnvironment &operator=(const PasswordEnvironment &) = delete;

private:
    std::optional<std::string> previous_;
};

Json wled_settings(const bool control = true, const bool global = false)
{
    return {{"timeout_seconds", 5},
            {"max_response_bytes", 65536},
            {"devices", Json::array({{{"name", "desk"},
                                      {"base_url", WLED},
                                      {"allow_http", true},
                                      {"allow_control", control},
                                      {"allow_global", global},
                                      {"max_brightness", 128},
                                      {"max_transition_deciseconds", 100},
                                      {"segments", Json::array({0})},
                                      {"presets", Json::array()}}})}};
}

Json shelly_settings(const bool control = true, const bool password = true)
{
    Json device = {{"base_url", SHELLY},
                   {"allow_http", true},
                   {"expected_device_id", SHELLY_ID},
                   {"expected_model", "SNPL-00112EU"},
                   {"expected_generation", 2},
                   {"switch_ids", Json::array({0})},
                   {"allow_control", control},
                   {"controls", Json::object()}};
    if (password)
    {
        device["password"] = "env:" + PASSWORD_VARIABLE;
    }
    if (control)
    {
        device["controls"]["0"] = {{"safe_load", "nonessential_lighting"}, {"max_power_watts", 60}, {"safety_acknowledged", true}};
    }
    return {{"timeout_seconds", 5}, {"devices", {{"lamp", device}}}};
}

Json root_configuration(const std::filesystem::path &directory, const Json &settings, const std::vector<std::string> &enabled)
{
    return {{"switchboard", {{"connectors", enabled}, {"data_dir", directory.string()}}},
            {"connectors", settings},
            {"policy", {{"tools", Json::object()}}},
            {"mcp", {{"expose", {"*"}}}}};
}

struct Fixture
{
    ProjectDirectory directory;
    Json configuration;
    Database database;
    FakeHttp http;
    Context context;
    Registry registry;

    Fixture(const Json &settings, const std::vector<std::string> &enabled)
        : configuration(root_configuration(directory.path, settings, enabled)), database(directory.path / "native-device-tests.sqlite3"),
          context(configuration, directory.path, database, http,
                  []()
                  {
                      return 1700000000;
                  })
    {
        register_devices(context, registry);
    }

    Json call(const std::string &name, const Json &arguments = Json::object())
    {
        const auto *tool = registry.find(name);
        expect(tool != nullptr, "expected authored tool to be registered");
        return tool->handler(arguments);
    }
};

void fails(const std::function<void()> &operation, const std::string &code)
{
    bool caught = false;
    try
    {
        operation();
    }
    catch (const Error &error)
    {
        caught = true;
        expect(error.code() == code, "unexpected sanitized error category");
        const std::string message = error.what();
        expect(message.find(PASSWORD) == std::string::npos && message.find(SHELLY) == std::string::npos &&
                   message.find("provider-private-details") == std::string::npos,
               "sanitized errors must withhold credentials and provider details");
    }
    expect(caught, "expected operation to fail closed");
}

Json wled_state()
{
    return {
        {"state",
         {{"on", true},
          {"bri", 64},
          {"transition", 7},
          {"pl", -1},
          {"nl", {{"on", false}}},
          {"seg",
           Json::array({{{"id", 0}, {"start", 0}, {"stop", 20}, {"on", true}, {"bri", 96}, {"col", Json::array({Json::array({8, 16, 24, 0})})}, {"fx", 0}}})}}},
        {"info", {{"ver", "0.15.0"}, {"live", false}, {"leds", {{"count", 20}, {"maxseg", 16}, {"seglc", Json::array({3})}}}}}};
}

Json shelly_status(const bool on = false, const Json &power = 12.5)
{
    return {{"id", 0},
            {"output", on},
            {"apower", power},
            {"voltage", 230.0},
            {"current", 0.1},
            {"aenergy", {{"total", 123.5}, {"by_minute", Json::array({1000, 2000, 3000})}}},
            {"ret_aenergy", {{"total", 2.5}}}};
}

Json frame(const std::int64_t id, const Json &result)
{
    return {{"id", id}, {"src", SHELLY_ID}, {"dst", "switchboard-native"}, {"result", result}};
}

void identify(Fixture &fixture, const bool authentication = true)
{
    fixture.http.add("POST", SHELLY + "/rpc", frame(1, {{"id", SHELLY_ID}, {"model", "SNPL-00112EU"}, {"gen", 2}, {"auth_en", authentication}}));
}

std::string challenge(const std::string &nonce = "AAAAAA-token_123+/=")
{
    return "Digest qop=\"auth\", realm=\"" + SHELLY_ID + "\", nonce=\"" + nonce + "\", algorithm=SHA-256";
}

void unauthorized(Fixture &fixture, const std::string &header)
{
    fixture.http.routes.push_back({"POST", SHELLY + "/rpc", {401, {{"WWW-Authenticate", header}}, ""}});
}

void protected_status(Fixture &fixture, const std::int64_t id, const Json &status)
{
    unauthorized(fixture, challenge());
    fixture.http.add("POST", SHELLY + "/rpc", frame(id, status));
}

void shelly_action(Fixture &fixture, const Json &after, const Json &acknowledgement = {{"was_on", false}}, const Json &before = shelly_status())
{
    identify(fixture);
    protected_status(fixture, 2, before);
    fixture.http.add("POST", SHELLY + "/rpc", frame(3, acknowledgement));
    protected_status(fixture, 4, after);
}

void wled_action(Fixture &fixture, const Json &after, const Json &acknowledgement = {{"success", true}}, const Json &before = wled_state())
{
    fixture.http.add("GET", WLED + "/json/si", before);
    fixture.http.add("POST", WLED + "/json/state", acknowledgement);
    fixture.http.add("GET", WLED + "/json/si", after);
}

std::string wled_target(Fixture &fixture, const std::optional<std::int64_t> segment = 0)
{
    const auto inventory = fixture.call("wled_devices");
    for (const auto &target : inventory.at("devices").at(0).at("targets"))
    {
        if ((segment && target.value("segment", -1) == *segment) || (!segment && target.at("scope") == "global"))
        {
            return target.at("target_digest").get<std::string>();
        }
    }
    throw std::runtime_error("authored inventory target not found");
}

Json wled_power(Fixture &fixture, const bool on = false)
{
    return {{"device", "desk"}, {"segment", 0}, {"target_digest", wled_target(fixture)}, {"on", on}};
}

Json shelly_output(Fixture &fixture, const bool on = true)
{
    return {{"device", "lamp"}, {"switch_id", 0}, {"on", on}, {"target", fixture.call("shelly_devices").at("devices").at(0).at("controls").at(0).at("target")}};
}

Json preset_settings(const Json &preset)
{
    auto settings = wled_settings();
    settings["devices"][0]["presets"] = Json::array({{{"name", "warm"}, {"id", 1}, {"sha256", hash(preset)}}});
    return settings;
}

Json preset_arguments(Fixture &fixture)
{
    return {
        {"device", "desk"}, {"preset", "warm"}, {"target_digest", fixture.call("wled_devices").at("devices").at(0).at("presets").at(0).at("target_digest")}};
}

std::size_t wled_posts(const Fixture &fixture)
{
    return static_cast<std::size_t>(std::count_if(fixture.http.requests.begin(), fixture.http.requests.end(),
                                                  [](const HttpRequest &request)
                                                  {
                                                      return request.method == "POST";
                                                  }));
}

std::size_t shelly_sets(const Fixture &fixture)
{
    return static_cast<std::size_t>(std::count_if(fixture.http.requests.begin(), fixture.http.requests.end(),
                                                  [](const HttpRequest &request)
                                                  {
                                                      return Json::parse(request.body).at("method") == "Switch.Set";
                                                  }));
}

void no_device_tables(Fixture &fixture)
{
    const auto tables = fixture.database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND (name LIKE 'wled_%' OR name LIKE 'shelly_%')");
    expect(tables.empty(), "device adapters must not create connector tables");
    expect(fixture.context.jobs.empty(), "device adapters must not schedule background work");
}

using Case = std::pair<std::string, std::function<void()>>;

std::vector<Case> authored_cases()
{
    return {
        {"disabled families ignore their settings",
         []()
         {
             Fixture fixture({{"wled", "invalid"}, {"shelly", "invalid"}}, {});
             expect(fixture.registry.tools().empty() && fixture.http.requests.empty(), "disabled adapters must do nothing");
         }},
        {"independent family activation",
         []()
         {
             Fixture wled({{"wled", wled_settings(false)}, {"shelly", "invalid"}}, {"wled"});
             Fixture shelly({{"shelly", shelly_settings(false)}, {"wled", "invalid"}}, {"shelly"});
             expect(wled.registry.find("wled_status") != nullptr && wled.registry.find("shelly_status") == nullptr, "WLED activation must be independent");
             expect(shelly.registry.find("shelly_status") != nullptr && shelly.registry.find("wled_status") == nullptr,
                    "Shelly activation must be independent");
         }},
        {"offline metadata resolves no credentials",
         []()
         {
             PasswordEnvironment environment(std::nullopt);
             Fixture fixture({{"wled", wled_settings(false)}, {"shelly", shelly_settings(false)}}, {"wled", "shelly"});
             const auto lights = fixture.call("wled_devices");
             const auto plugs = fixture.call("shelly_devices");
             expect(lights.at("configured_only") == true && plugs.at("live_validation") == false &&
                        plugs.at("devices").at(0).at("authentication_configured") == true,
                    "metadata must identify its offline limits");
             expect(fixture.http.requests.empty(), "registration and inventory must be offline");
             const auto output = lights.dump() + plugs.dump();
             expect(output.find("192.168.") == std::string::npos && output.find(PASSWORD_VARIABLE) == std::string::npos,
                    "offline metadata must withhold endpoints and secret references");
             no_device_tables(fixture);
         }},
        {"offline file reference and empty inventory arguments",
         []()
         {
             ProjectDirectory unused_secret;
             auto settings = shelly_settings(false);
             settings["devices"]["lamp"]["password"] = "file:" + (unused_secret.path / "absent-password").string();
             Fixture fixture({{"wled", wled_settings(false)}, {"shelly", settings}}, {"wled", "shelly"});
             expect(fixture.call("shelly_devices").at("devices").size() == 1, "an absent credential file must not affect inventory");
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_devices", {{"device", "lamp"}});
                 },
                 "devices_arguments");
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_devices", {{"discover", true}});
                 },
                 "devices_arguments");
             expect(fixture.http.requests.empty(), "inventory must not resolve secrets or accept discovery flags");
         }},
        {"tool tiers and read-only opt-ins",
         []()
         {
             Fixture readonly({{"wled", wled_settings(false)}, {"shelly", shelly_settings(false)}}, {"wled", "shelly"});
             expect(readonly.registry.tools().size() == 4 && readonly.registry.find("wled_set_power") == nullptr &&
                        readonly.registry.find("shelly_switch_set") == nullptr,
                    "read-only configuration must omit actions");
             Fixture controlled({{"wled", wled_settings()}, {"shelly", shelly_settings()}}, {"wled", "shelly"});
             for (const auto &[name, tool] : controlled.registry.tools())
             {
                 const bool action = name.find("_set") != std::string::npos;
                 expect(tool.tier == (action ? Tier::act : Tier::read), "controls must use the approval-gated tier");
             }
             expect(controlled.registry.find("wled_set_transition") == nullptr && controlled.registry.find("wled_apply_preset") == nullptr,
                    "unavailable advanced scopes must not be stubbed");
         }},
        {"strict unknown settings and types",
         []()
         {
             auto light = wled_settings();
             light["arbitrary"] = "provider-private-details";
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
             auto plug = shelly_settings();
             plug["devices"]["lamp"]["allow_control"] = 1;
             fails(
                 [&]()
                 {
                     Fixture fixture({{"shelly", plug}}, {"shelly"});
                 },
                 "devices_config");
         }},
        {"plain transport requires per-device consent",
         []()
         {
             auto light = wled_settings();
             light["devices"][0]["allow_http"] = false;
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
             auto plug = shelly_settings(false);
             plug["devices"]["lamp"]["allow_http"] = false;
             fails(
                 [&]()
                 {
                     Fixture fixture({{"shelly", plug}}, {"shelly"});
                 },
                 "devices_config");
         }},
        {"origin confinement and credential rejection",
         []()
         {
             for (const std::string endpoint : {"http://user:pass@192.168.50.20", "http://192.168.50.20/path", "http://192.168.50.20?x=1",
                                                "http://192.168.50.20#x", "http://192.168.50.20/%2f", "ftp://192.168.50.20", "http://8.8.8.8"})
             {
                 auto light = wled_settings();
                 light["devices"][0]["base_url"] = endpoint;
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"wled", light}}, {"wled"});
                     },
                     "devices_config");
             }
             for (const std::string endpoint : {"http://plug.local", "http://127.0.0.1", "http://169.254.1.1", "http://8.8.8.8"})
             {
                 auto plug = shelly_settings(false);
                 plug["devices"]["lamp"]["base_url"] = endpoint;
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"shelly", plug}}, {"shelly"});
                     },
                     "devices_config");
             }
         }},
        {"normalized duplicate origins are rejected",
         []()
         {
             auto light = wled_settings();
             auto duplicate = light.at("devices").at(0);
             duplicate["name"] = "other";
             duplicate["base_url"] = WLED + ":80/";
             light["devices"].push_back(duplicate);
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
         }},
        {"bounded device and segment configuration",
         []()
         {
             auto light = wled_settings();
             light["devices"][0]["segments"] = Json::array({0, 0});
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
             light["devices"][0]["segments"] = Json::array({64});
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
             light = wled_settings();
             while (light["devices"].size() <= 16)
             {
                 light["devices"].push_back(light["devices"][0]);
             }
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
         }},
        {"transport configuration caps",
         []()
         {
             for (const Json &value : Json::array({0, 31, 1.5, true}))
             {
                 auto light = wled_settings();
                 light["timeout_seconds"] = value;
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"wled", light}}, {"wled"});
                     },
                     "devices_config");
             }
             auto light = wled_settings();
             light["max_response_bytes"] = 262145;
             fails(
                 [&]()
                 {
                     Fixture fixture({{"wled", light}}, {"wled"});
                 },
                 "devices_config");
             auto plug = shelly_settings();
             plug["timeout_seconds"] = 16;
             fails(
                 [&]()
                 {
                     Fixture fixture({{"shelly", plug}}, {"shelly"});
                 },
                 "devices_config");
         }},
        {"WLED exact status request and minimization",
         []()
         {
             auto settings = wled_settings(false);
             settings["timeout_seconds"] = 9;
             settings["max_response_bytes"] = 8192;
             Fixture fixture({{"wled", settings}}, {"wled"});
             auto state = wled_state();
             state["info"]["mac"] = "provider-private-details";
             auto other = state["state"]["seg"][0];
             other["id"] = 1;
             state["state"]["seg"].push_back(other);
             fixture.http.add("GET", WLED + "/json/si", state);
             const auto result = fixture.call("wled_status", {{"device", "desk"}});
             const auto &request = fixture.http.requests.at(0);
             expect(request.method == "GET" && request.url == WLED + "/json/si" && request.body.empty() &&
                        request.headers == std::map<std::string, std::string>{{"Accept", "application/json"}} && request.allow_http &&
                        request.timeout_seconds == 9 && request.max_bytes == 8192 && !request.digest_password && !request.digest_realm,
                    "exact bounded WLED status request");
             expect(result.at("segments").size() == 1 && result.at("observed_at") == timestamp(1700000000) &&
                        result.dump().find("provider-private-details") == std::string::npos,
                    "status must be minimized and host-timestamped");
         }},
        {"WLED exact absolute segment power and readback",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto after = wled_state();
             after["state"]["seg"][0]["on"] = false;
             wled_action(fixture, after);
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             const Json expected = {{"seg", Json::array({{{"id", 0}, {"on", false}}})}, {"udpn", {{"nn", true}}}};
             expect(Json::parse(fixture.http.requests.at(1).body) == expected && fixture.http.requests.at(1).headers.at("Content-Type") == "application/json",
                    "power must be one absolute patch with notifications suppressed");
             expect(result.at("status") == "state_matched" && result.at("acknowledged") == true && result.at("matches_requested") == true &&
                        result.at("physical_output_verified") == false && wled_posts(fixture) == 1,
                    "matching state is not physical confirmation or replay");
         }},
        {"WLED global opt-in and target separation",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto arguments = wled_power(fixture);
             arguments.erase("segment");
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", arguments);
                 },
                 "devices_binding");
             expect(fixture.http.requests.empty(), "a segment binding cannot authorize global output");
             Fixture global({{"wled", wled_settings(true, true)}}, {"wled"});
             auto after = wled_state();
             after["state"]["on"] = false;
             wled_action(global, after);
             const auto result = global.call("wled_set_power", {{"device", "desk"}, {"target_digest", wled_target(global, std::nullopt)}, {"on", false}});
             expect(result.at("status") == "state_matched" && Json::parse(global.http.requests.at(1).body) == Json({{"on", false}, {"udpn", {{"nn", true}}}}),
                    "global control must require its own configured binding");
         }},
        {"WLED brightness cap and no implicit power-on",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto after = wled_state();
             after["state"]["seg"][0]["bri"] = 100;
             wled_action(fixture, after);
             const auto result =
                 fixture.call("wled_set_brightness", {{"device", "desk"}, {"segment", 0}, {"target_digest", wled_target(fixture)}, {"brightness", 100}});
             const auto payload = Json::parse(fixture.http.requests.at(1).body);
             expect(payload.at("seg").at(0).at("bri") == 100 && !payload.at("seg").at(0).contains("on") && result.at("status") == "state_matched",
                    "brightness must not add an implicit power command");
             Fixture capped({{"wled", wled_settings()}}, {"wled"});
             capped.http.add("GET", WLED + "/json/si", wled_state());
             fails(
                 [&]()
                 {
                     (void)capped.call("wled_set_brightness",
                                       {{"device", "desk"}, {"segment", 0}, {"target_digest", wled_target(capped)}, {"brightness", 129}});
                 },
                 "devices_safety");
             expect(wled_posts(capped) == 0, "configured brightness cap must refuse before writing");
         }},
        {"WLED recalled brightness and global bounds safety",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto before = wled_state();
             before["state"]["bri"] = 200;
             fixture.http.add("GET", WLED + "/json/si", before);
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", wled_power(fixture, true));
                 },
                 "devices_safety");
             expect(wled_posts(fixture) == 0, "turning on must not recall brightness above the cap");
             Fixture global({{"wled", wled_settings(true, true)}}, {"wled"});
             auto unknown_bounds = wled_state();
             unknown_bounds["state"]["seg"][0].erase("stop");
             global.http.add("GET", WLED + "/json/si", unknown_bounds);
             fails(
                 [&]()
                 {
                     (void)global.call("wled_set_power", {{"device", "desk"}, {"on", true}, {"target_digest", wled_target(global, std::nullopt)}});
                 },
                 "devices_safety");
             expect(wled_posts(global) == 0, "global power-on must not treat unreported bounds as a known inactive segment");
         }},
        {"WLED static color payload and channel normalization",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto after = wled_state();
             after["state"]["seg"][0]["col"] = Json::array({Json::array({9, 8, 7, 0})});
             wled_action(fixture, after);
             const auto result = fixture.call("wled_set_color",
                                              {{"device", "desk"}, {"segment", 0}, {"target_digest", wled_target(fixture)}, {"color", Json::array({9, 8, 7})}});
             const auto payload = Json::parse(fixture.http.requests.at(1).body);
             expect(payload.at("seg").at(0).at("fx") == 0 && !payload.contains("on") && result.at("matches_requested") == true,
                    "color must select Solid and normalize an absent white channel to zero");
         }},
        {"WLED per-segment capability indexing",
         []()
         {
             auto settings = wled_settings();
             settings["devices"][0]["segments"] = Json::array({2});
             Fixture fixture({{"wled", settings}}, {"wled"});
             auto before = wled_state();
             before["state"]["seg"][0]["id"] = 2;
             before["info"]["leds"]["seglc"] = Json::array({1, 1, 3});
             auto after = before;
             after["state"]["seg"][0]["col"] = Json::array({Json::array({1, 2, 3, 4})});
             wled_action(fixture, after, {{"success", true}}, before);
             const auto result = fixture.call(
                 "wled_set_color", {{"device", "desk"}, {"segment", 2}, {"target_digest", wled_target(fixture, 2)}, {"color", Json::array({1, 2, 3, 4})}});
             expect(result.at("matches_requested") == true, "white-channel capability must be indexed by segment identifier");
         }},
        {"WLED unknown or absent capabilities fail closed",
         []()
         {
             for (const int mode : {0, 1, 2})
             {
                 Fixture fixture({{"wled", wled_settings()}}, {"wled"});
                 auto before = wled_state();
                 if (mode == 0)
                 {
                     before["info"]["leds"].erase("seglc");
                 }
                 else
                 {
                     before["info"]["leds"]["seglc"] = Json::array({mode == 1 ? 1 : 0});
                 }
                 fixture.http.add("GET", WLED + "/json/si", before);
                 fails(
                     [&]()
                     {
                         (void)fixture.call(
                             "wled_set_color",
                             {{"device", "desk"}, {"segment", 0}, {"target_digest", wled_target(fixture)}, {"color", Json::array({1, 2, 3, 4})}});
                     },
                     "devices_safety");
                 expect(wled_posts(fixture) == 0, "unreported or unsupported channels must not be written");
             }
         }},
        {"WLED transition is global persistent deciseconds",
         []()
         {
             auto settings = wled_settings(true, true);
             settings["devices"][0]["max_transition_deciseconds"] = 20;
             Fixture fixture({{"wled", settings}}, {"wled"});
             auto after = wled_state();
             after["state"]["transition"] = 4;
             wled_action(fixture, after);
             const auto result = fixture.call("wled_set_transition",
                                              {{"device", "desk"}, {"target_digest", wled_target(fixture, std::nullopt)}, {"transition_deciseconds", 4}});
             expect(result.at("status") == "state_matched" &&
                        Json::parse(fixture.http.requests.at(1).body) == Json({{"transition", 4}, {"udpn", {{"nn", true}}}}),
                    "transition must use the readable persistent field, not tt or milliseconds");
         }},
        {"WLED transition cap is enforced before write",
         []()
         {
             auto settings = wled_settings(true, true);
             settings["devices"][0]["max_transition_deciseconds"] = 4;
             Fixture fixture({{"wled", settings}}, {"wled"});
             fixture.http.add("GET", WLED + "/json/si", wled_state());
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_transition",
                                        {{"device", "desk"}, {"target_digest", wled_target(fixture, std::nullopt)}, {"transition_deciseconds", 5}});
                 },
                 "devices_safety");
             expect(wled_posts(fixture) == 0, "per-device transition cap must be effective");
         }},
        {"WLED absent and active overriding modes refuse",
         []()
         {
             for (const int mode : {0, 1, 2, 3})
             {
                 Fixture fixture({{"wled", wled_settings()}}, {"wled"});
                 auto before = wled_state();
                 if (mode == 0)
                 {
                     before["info"].erase("live");
                 }
                 else if (mode == 1)
                 {
                     before["info"]["live"] = true;
                 }
                 else if (mode == 2)
                 {
                     before["state"]["pl"] = 1;
                 }
                 else
                 {
                     before["state"]["nl"]["on"] = true;
                 }
                 fixture.http.add("GET", WLED + "/json/si", before);
                 fails(
                     [&]()
                     {
                         (void)fixture.call("wled_set_power", wled_power(fixture));
                     },
                     "devices_safety");
                 expect(wled_posts(fixture) == 0, "unknown or active overriding modes must not be overridden");
             }
         }},
        {"WLED inactive missing and duplicate segments",
         []()
         {
             for (const int mode : {0, 1, 2})
             {
                 Fixture fixture({{"wled", wled_settings()}}, {"wled"});
                 auto before = wled_state();
                 if (mode == 0)
                 {
                     before["state"]["seg"][0]["stop"] = 0;
                 }
                 else if (mode == 1)
                 {
                     before["state"]["seg"] = Json::array();
                 }
                 else
                 {
                     before["state"]["seg"].push_back(before["state"]["seg"][0]);
                 }
                 fixture.http.add("GET", WLED + "/json/si", before);
                 fails(
                     [&]()
                     {
                         (void)fixture.call("wled_set_power", wled_power(fixture));
                     },
                     mode == 2 ? "devices_protocol" : "devices_safety");
                 expect(wled_posts(fixture) == 0, "invalid target segments must not produce writes");
             }
         }},
        {"WLED action arguments cannot add arbitrary commands",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto arguments = wled_power(fixture);
             arguments["rb"] = true;
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", arguments);
                 },
                 "devices_arguments");
             arguments = wled_power(fixture);
             arguments["on"] = "t";
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", arguments);
                 },
                 "devices_arguments");
             arguments = wled_power(fixture);
             arguments.erase("target_digest");
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", arguments);
                 },
                 "devices_arguments");
             expect(fixture.http.requests.empty(), "unknown fields, toggle and missing bindings must fail offline");
         }},
        {"WLED stale safety binding is refused offline",
         []()
         {
             Fixture original({{"wled", wled_settings()}}, {"wled"});
             const auto arguments = wled_power(original);
             auto changed = wled_settings();
             changed["devices"][0]["max_brightness"] = 127;
             Fixture replacement({{"wled", changed}}, {"wled"});
             fails(
                 [&]()
                 {
                     (void)replacement.call("wled_set_power", arguments);
                 },
                 "devices_arguments");
             expect(replacement.http.requests.empty(), "stale configuration must be refused before transport");
         }},
        {"WLED cross-device binding is refused offline",
         []()
         {
             auto settings = wled_settings();
             auto other = settings["devices"][0];
             other["name"] = "shelf";
             other["base_url"] = "http://192.168.50.21";
             settings["devices"].push_back(other);
             Fixture fixture({{"wled", settings}}, {"wled"});
             auto arguments = wled_power(fixture);
             arguments["device"] = "shelf";
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_set_power", arguments);
                 },
                 "devices_binding");
             expect(fixture.http.requests.empty(), "a valid digest from another named device must not authorize it");
         }},
        {"WLED matching acknowledgement is not matching state",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             wled_action(fixture, wled_state());
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             expect(result.at("acknowledged") == true && result.at("matches_requested") == false && result.at("status") == "state_mismatch",
                    "acknowledgement must not hide a mismatching readback");
         }},
        {"WLED malformed acknowledgement retains uncertainty",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             auto after = wled_state();
             after["state"]["seg"][0]["on"] = false;
             wled_action(fixture, after, {{"unexpected", true}});
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             expect(result.at("acknowledged").is_null() && result.at("matches_requested") == true && result.at("status") == "uncertain" &&
                        result.at("command_error") == "devices_protocol",
                    "matching state must not convert an unknown command acceptance into success");
         }},
        {"WLED provider errors are sanitized and not replayed",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             wled_action(fixture, wled_state(), {{"error", "provider-private-details"}});
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             expect(result.at("status") == "rejected" && result.at("acknowledged") == false && result.at("command_error") == "devices_provider" &&
                        wled_posts(fixture) == 1 && result.dump().find("provider-private-details") == std::string::npos,
                    "provider rejection must be bounded and must not trigger a replay");
         }},
        {"WLED transport uncertainty still permits one read only",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             fixture.http.add("GET", WLED + "/json/si", wled_state());
             auto after = wled_state();
             after["state"]["seg"][0]["on"] = false;
             fixture.http.add("GET", WLED + "/json/si", after);
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             expect(result.at("status") == "uncertain" && result.at("command_error") == "devices_transport" && result.at("matches_requested") == true &&
                        fixture.http.requests.size() == 3 && wled_posts(fixture) == 1,
                    "a failed fake write must not be replayed even if a subsequent read matches");
         }},
        {"WLED unavailable readback is not preflight fallback",
         []()
         {
             Fixture fixture({{"wled", wled_settings()}}, {"wled"});
             fixture.http.add("GET", WLED + "/json/si", wled_state());
             fixture.http.add("POST", WLED + "/json/state", {{"success", true}});
             const auto result = fixture.call("wled_set_power", wled_power(fixture));
             expect(result.at("status") == "unconfirmed" && result.at("observation").is_null() && result.at("matches_requested").is_null() &&
                        result.at("readback_error") == "devices_transport",
                    "a failed readback must never use the preflight state");
         }},
        {"WLED redirects and byte caps are enforced in adapter",
         []()
         {
             Fixture redirect({{"wled", wled_settings(false)}}, {"wled"});
             redirect.http.routes.push_back({"GET", WLED + "/json/si", {302, {{"Location", "http://8.8.8.8"}}, ""}});
             fails(
                 [&]()
                 {
                     (void)redirect.call("wled_status", {{"device", "desk"}});
                 },
                 "devices_redirect");
             expect(redirect.http.requests.size() == 1, "redirects must not be followed");
             Fixture oversized({{"wled", wled_settings(false)}}, {"wled"});
             oversized.http.routes.push_back({"GET", WLED + "/json/si", {200, {}, std::string(65537, 'x')}});
             fails(
                 [&]()
                 {
                     (void)oversized.call("wled_status", {{"device", "desk"}});
                 },
                 "devices_protocol");
         }},
        {"WLED reviewed static preset copies state not identifier",
         []()
         {
             const Json preset = {{"n", "Warm"}, {"seg", Json::array({{{"id", 0}, {"bri", 80}, {"col", Json::array({Json::array({4, 5, 6})})}, {"fx", 0}}})}};
             Fixture fixture({{"wled", preset_settings(preset)}}, {"wled"});
             fixture.http.add("GET", WLED + "/presets.json", {{"1", preset}});
             auto after = wled_state();
             after["state"]["seg"][0]["bri"] = 80;
             after["state"]["seg"][0]["col"] = Json::array({Json::array({4, 5, 6, 0})});
             wled_action(fixture, after);
             const auto result = fixture.call("wled_apply_preset", preset_arguments(fixture));
             const auto payload = Json::parse(fixture.http.requests.at(2).body);
             expect(result.at("status") == "state_matched" && !payload.contains("ps") && !payload.contains("n") && payload.at("seg") == preset.at("seg"),
                    "a reviewed preset must be copied, never executed by id");
         }},
        {"WLED changed preset fingerprint refuses before state write",
         []()
         {
             const Json preset = {{"seg", Json::array({{{"id", 0}, {"on", false}}})}};
             Fixture fixture({{"wled", preset_settings(preset)}}, {"wled"});
             auto changed = preset;
             changed["n"] = "Edited";
             fixture.http.add("GET", WLED + "/presets.json", {{"1", changed}});
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_apply_preset", preset_arguments(fixture));
                 },
                 "devices_binding");
             expect(fixture.http.requests.size() == 1 && wled_posts(fixture) == 0, "metadata edits require new review too");
         }},
        {"WLED preset macros geometry and dynamic values fail closed",
         []()
         {
             for (const Json &preset :
                  Json::array({Json{{"ps", 1}}, Json{{"playlist", {{"ps", Json::array({1, 2})}}}},
                               Json{{"seg", Json::array({{{"id", 0}, {"start", 0}, {"on", true}}})}}, Json{{"seg", Json::array({{{"id", 0}, {"on", "t"}}})}},
                               Json{{"seg", Json::array({{{"id", 0}, {"bri", "~10"}}})}},
                               Json{{"seg", Json::array({{{"id", 0}, {"col", Json::array({Json::array({1, 2, 3})})}}})}}}))
             {
                 Fixture fixture({{"wled", preset_settings(preset)}}, {"wled"});
                 fixture.http.add("GET", WLED + "/presets.json", {{"1", preset}});
                 fails(
                     [&]()
                     {
                         (void)fixture.call("wled_apply_preset", preset_arguments(fixture));
                     },
                     "devices_safety");
                 expect(wled_posts(fixture) == 0, "even fingerprint-matching unsupported preset content must be refused");
             }
         }},
        {"WLED preset and segment bindings are not interchangeable",
         []()
         {
             const Json preset = {{"seg", Json::array({{{"id", 0}, {"on", false}}})}};
             Fixture fixture({{"wled", preset_settings(preset)}}, {"wled"});
             auto arguments = preset_arguments(fixture);
             arguments["target_digest"] = wled_target(fixture);
             fails(
                 [&]()
                 {
                     (void)fixture.call("wled_apply_preset", arguments);
                 },
                 "devices_arguments");
             expect(fixture.http.requests.empty(), "preset selection requires its own inventory binding");
         }},
        {"Shelly unsupported models generations and components fail offline",
         []()
         {
             for (const int mode : {0, 1, 2})
             {
                 auto settings = shelly_settings(false);
                 if (mode == 0)
                 {
                     settings["devices"]["lamp"]["expected_model"] = "UNREVIEWED_MODEL";
                 }
                 else if (mode == 1)
                 {
                     settings["devices"]["lamp"]["expected_generation"] = 3;
                 }
                 else
                 {
                     settings["devices"]["lamp"]["switch_ids"] = Json::array({1});
                 }
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"shelly", settings}}, {"shelly"});
                     },
                     "devices_config");
             }
         }},
        {"Shelly controls require complete safe-load opt-in and password",
         []()
         {
             for (const int mode : {0, 1, 2, 3, 4, 5})
             {
                 auto settings = shelly_settings();
                 auto &device = settings["devices"]["lamp"];
                 if (mode == 0)
                 {
                     device["allow_control"] = false;
                 }
                 else if (mode == 1)
                 {
                     device["controls"] = Json::object();
                 }
                 else if (mode == 2)
                 {
                     device["controls"]["0"]["safe_load"] = "heater";
                 }
                 else if (mode == 3)
                 {
                     device["controls"]["0"]["safety_acknowledged"] = false;
                 }
                 else if (mode == 4)
                 {
                     device["controls"]["0"]["max_power_watts"] = 101;
                 }
                 else
                 {
                     device.erase("password");
                 }
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"shelly", settings}}, {"shelly"});
                     },
                     "devices_config");
             }
         }},
        {"Shelly literal and command secret references are refused",
         []()
         {
             for (const std::string reference : {"literal-password", "cmd:echo secret", "env:", "env:BAD-NAME", "file:relative"})
             {
                 auto settings = shelly_settings(false);
                 settings["devices"]["lamp"]["password"] = reference;
                 fails(
                     [&]()
                     {
                         Fixture fixture({{"shelly", settings}}, {"shelly"});
                     },
                     "devices_config");
             }
         }},
        {"Shelly exact public identity and unauthenticated status frames",
         []()
         {
             Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
             identify(fixture, false);
             fixture.http.add("POST", SHELLY + "/rpc", frame(2, shelly_status()));
             const auto result = fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             const auto &request = fixture.http.requests.at(0);
             expect(request.method == "POST" && request.url == SHELLY + "/rpc" &&
                        Json::parse(request.body) ==
                            Json({{"id", 1}, {"src", "switchboard-native"}, {"method", "Shelly.GetDeviceInfo"}, {"params", {{"ident", false}}}}) &&
                        request.max_bytes == 65536 && request.timeout_seconds == 5 && request.allow_http && !request.digest_password && !request.digest_realm,
                    "identity uses the documented public fixed method");
             expect(Json::parse(fixture.http.requests.at(1).body).at("params") == Json({{"id", 0}}) && result.at("output") == false,
                    "component selection must remain fixed");
         }},
        {"Shelly units and cumulative energy are explicit",
         []()
         {
             Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
             identify(fixture, false);
             fixture.http.add("POST", SHELLY + "/rpc", frame(2, shelly_status()));
             const auto result = fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             expect(result.at("power").at("unit") == "W" && result.at("energy").at("unit") == "Wh" && result.at("energy").at("value") == 123.5 &&
                        result.at("returned_energy").at("value") == 2.5 && result.at("energy").at("kind") == "device_cumulative_counter" &&
                        !result.at("energy").contains("by_minute") && result.at("measurement_age_seconds").is_null() &&
                        result.at("observed_at") == timestamp(1700000000),
                    "total energy must not be confused with minute counters or billing");
         }},
        {"Shelly missing metering is unavailable rather than zero",
         []()
         {
             Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
             identify(fixture, false);
             fixture.http.add("POST", SHELLY + "/rpc", frame(2, {{"id", 0}, {"output", false}}));
             const auto result = fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             expect(result.at("power").at("available") == false && result.at("power").at("value").is_null() && result.at("energy").at("available") == false,
                    "missing measurements must remain unknown");
         }},
        {"Shelly malformed measurements and component output reject",
         []()
         {
             for (const int mode : {0, 1, 2, 3, 4})
             {
                 Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
                 identify(fixture, false);
                 auto raw = shelly_status();
                 if (mode == 0)
                 {
                     raw["apower"] = true;
                 }
                 else if (mode == 1)
                 {
                     raw["aenergy"]["total"] = -1;
                 }
                 else if (mode == 2)
                 {
                     raw["output"] = "false";
                 }
                 else if (mode == 3)
                 {
                     raw["id"] = 1;
                 }
                 else
                 {
                     raw["timer_type"] = "auto_off";
                 }
                 fixture.http.add("POST", SHELLY + "/rpc", frame(2, raw));
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     "devices_protocol");
             }
         }},
        {"Shelly identity model generation and auth shape are pinned",
         []()
         {
             for (const std::string field : {"id", "model", "gen", "auth_en"})
             {
                 Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
                 Json info = {{"id", SHELLY_ID}, {"model", "SNPL-00112EU"}, {"gen", 2}, {"auth_en", true}};
                 info[field] = field == "gen" ? Json(3) : field == "auth_en" ? Json("true") : Json("other");
                 fixture.http.add("POST", SHELLY + "/rpc", frame(1, info));
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     field == "auth_en" ? "devices_auth" : "devices_identity");
                 expect(fixture.http.requests.size() == 1, "identity mismatch must stop before protected operations");
             }
         }},
        {"Shelly configured authentication cannot silently disappear",
         []()
         {
             Fixture disabled({{"shelly", shelly_settings(false)}}, {"shelly"});
             identify(disabled, false);
             fails(
                 [&]()
                 {
                     (void)disabled.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                 },
                 "devices_auth");
             Fixture bypassed({{"shelly", shelly_settings(false)}}, {"shelly"});
             identify(bypassed);
             bypassed.http.add("POST", SHELLY + "/rpc", frame(2, shelly_status()));
             fails(
                 [&]()
                 {
                     (void)bypassed.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                 },
                 "devices_auth");
         }},
        {"Shelly validated challenge delegates Digest to native transport",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
             identify(fixture);
             protected_status(fixture, 2, shelly_status());
             const auto result = fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             expect(result.at("power").at("available") == true && fixture.http.requests.size() == 3,
                    "a definite read challenge permits one credentialed transport invocation");
             const auto &unsigned_request = fixture.http.requests.at(1);
             const auto &signed_transport = fixture.http.requests.at(2);
             expect(!unsigned_request.digest_password && !unsigned_request.digest_realm && signed_transport.digest_password == PASSWORD &&
                        signed_transport.digest_realm == SHELLY_ID && unsigned_request.body == signed_transport.body &&
                        !signed_transport.headers.contains("Authorization") && signed_transport.headers.at("Content-Type") == "application/json" &&
                        signed_transport.url == SHELLY + "/rpc",
                    "Digest must carry the pinned realm with the transport credential and preserve the actual POST body");
             expect(signed_transport.body.find(PASSWORD) == std::string::npos && result.dump().find(PASSWORD) == std::string::npos,
                    "credentials must not enter frames or returned data");
         }},
        {"Shelly numeric and token nonces are supported",
         []()
         {
             PasswordEnvironment environment;
             for (const std::string nonce : {"123456789", "AAAA_token+/=.-"})
             {
                 Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
                 identify(fixture);
                 unauthorized(fixture, challenge(nonce) + ", opaque=\"abc123\", stale=false, charset=UTF-8, userhash=false");
                 fixture.http.add("POST", SHELLY + "/rpc", frame(2, shelly_status()));
                 expect(fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}}).at("output") == false,
                        "documented nonce shapes must reach the native Digest transport");
             }
         }},
        {"Shared Digest parser accepts a new nonce within the pinned profile",
         []()
         {
             validate_digest_challenge(challenge("123456789"), SHELLY_ID);
             validate_digest_challenge(challenge("AAAA_new-token+/=") + ", opaque=\"abc123\", stale=true, charset=UTF-8, userhash=false", SHELLY_ID);
         }},
        {"Shared Digest parser refuses changed realm and downgraded profiles",
         []()
         {
             validate_digest_challenge(challenge("initial-nonce"), SHELLY_ID);
             const std::vector<std::string> rejected = {"Digest realm=\"other-device\", qop=auth, nonce=new_nonce, algorithm=SHA-256",
                                                        "Digest realm=\"" + SHELLY_ID + "\", qop=auth, nonce=new_nonce, algorithm=MD5",
                                                        "Digest realm=\"" + SHELLY_ID + "\", qop=auth, nonce=new_nonce, algorithm=SHA-256-sess",
                                                        "Digest realm=\"" + SHELLY_ID + "\", qop=auth-int, nonce=new_nonce, algorithm=SHA-256",
                                                        challenge("new_nonce") + ", realm=\"" + SHELLY_ID + "\"",
                                                        challenge("new_nonce") + ", opaque=\"invalid opaque\""};
             for (const auto &header : rejected)
             {
                 fails(
                     [&]()
                     {
                         validate_digest_challenge(header, SHELLY_ID);
                     },
                     "devices_auth");
             }
         }},
        {"Shelly unsupported challenges refuse before secret lookup",
         []()
         {
             PasswordEnvironment environment(std::nullopt);
             const std::vector<std::string> headers = {"Basic realm=\"private\"",
                                                       challenge() + ", algorithm=MD5",
                                                       "Digest realm=\"" + SHELLY_ID + "\", qop=auth, nonce=123, algorithm=MD5",
                                                       "Digest realm=\"other\", qop=auth, nonce=123, algorithm=SHA-256",
                                                       "Digest realm=\"" + SHELLY_ID + "\", qop=auth-int, nonce=123, algorithm=SHA-256",
                                                       challenge() + ", extension=unexpected",
                                                       challenge() + ", nonce=again"};
             for (const auto &header : headers)
             {
                 Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
                 identify(fixture);
                 unauthorized(fixture, header);
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     "devices_auth");
                 expect(fixture.http.requests.size() == 2, "unsupported auth must fail before credential resolution or resend");
             }
         }},
        {"Shelly missing challenge headers fail closed",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
             identify(fixture);
             fixture.http.routes.push_back({"POST", SHELLY + "/rpc", {401, {}, ""}});
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                 },
                 "devices_auth");
             expect(fixture.http.requests.size() == 2, "transport must preserve WWW-Authenticate for safe negotiation");
         }},
        {"Shelly unavailable or malformed credentials are sanitized",
         []()
         {
             for (const auto &password :
                  {std::optional<std::string>(), std::optional<std::string>("bad\npassword"), std::optional<std::string>(std::string(1025, 'x'))})
             {
                 PasswordEnvironment environment(password);
                 Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
                 identify(fixture);
                 unauthorized(fixture, challenge());
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     "devices_secret");
                 expect(fixture.http.requests.size() == 2, "an invalid credential must never reach the native transport");
             }
         }},
        {"Shelly file references resolve lazily through Context",
         []()
         {
             ProjectDirectory secret_directory;
             const auto path = secret_directory.path / "device-password";
             {
                 std::ofstream output(path);
                 output << PASSWORD;
                 expect(output.good(), "cannot author synthetic private credential");
             }
             expect(chmod(path.c_str(), 0600) == 0, "cannot protect synthetic credential");
             auto settings = shelly_settings(false);
             settings["devices"]["lamp"]["password"] = "file:" + path.string();
             Fixture fixture({{"shelly", settings}}, {"shelly"});
             expect(fixture.http.requests.empty(), "file credentials must not be resolved during registration");
             identify(fixture);
             protected_status(fixture, 2, shelly_status());
             (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             expect(fixture.http.requests.at(2).digest_password == PASSWORD && fixture.http.requests.at(2).digest_realm == SHELLY_ID,
                    "only Context may resolve a lazy file credential, paired with the pinned realm");
         }},
        {"Shelly authentication rejection is bounded",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings(false)}}, {"shelly"});
             identify(fixture);
             unauthorized(fixture, challenge());
             unauthorized(fixture, challenge("different-nonce"));
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                 },
                 "devices_auth");
             expect(fixture.http.requests.size() == 3, "a second unauthorized response must not loop or downgrade");
         }},
        {"Shelly full frame correlation and provider redaction",
         []()
         {
             for (const int mode : {0, 1, 2, 3})
             {
                 Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
                 identify(fixture, false);
                 auto response = frame(2, shelly_status());
                 if (mode == 0)
                 {
                     response["id"] = 3;
                 }
                 else if (mode == 1)
                 {
                     response["src"] = "other";
                 }
                 else if (mode == 2)
                 {
                     response["dst"] = "other";
                 }
                 else
                 {
                     response.erase("result");
                     response["error"] = {{"code", -105}, {"message", "provider-private-details"}};
                 }
                 fixture.http.add("POST", SHELLY + "/rpc", response);
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     mode == 3 ? "devices_provider" : "devices_protocol");
             }
         }},
        {"Shelly unsafe power errors and active timers refuse both outputs",
         []()
         {
             PasswordEnvironment environment;
             for (const int mode : {0, 1, 2, 3, 4})
             {
                 Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
                 auto before = shelly_status();
                 if (mode == 0)
                 {
                     before.erase("apower");
                 }
                 else if (mode == 1)
                 {
                     before["apower"] = -1;
                 }
                 else if (mode == 2)
                 {
                     before["apower"] = 61;
                 }
                 else if (mode == 3)
                 {
                     before["errors"] = Json::array({"overtemp"});
                 }
                 else
                 {
                     before["timer_started_at"] = 1700000000;
                     before["timer_duration"] = 60;
                 }
                 identify(fixture);
                 protected_status(fixture, 2, before);
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_switch_set", shelly_output(fixture, mode % 2 == 0));
                     },
                     "devices_safety");
                 expect(shelly_sets(fixture) == 0, "unsafe preflight must not write either ON or OFF");
             }
         }},
        {"Shelly exact absolute output acceptance and matching safe state",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             shelly_action(fixture, shelly_status(true));
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             const auto &request = fixture.http.requests.at(3);
             expect(Json::parse(request.body) ==
                            Json({{"id", 3}, {"src", "switchboard-native"}, {"method", "Switch.Set"}, {"params", {{"id", 0}, {"on", true}}}}) &&
                        request.digest_password == PASSWORD && request.digest_realm == SHELLY_ID && request.method == "POST",
                    "the only output request is a credentialed, complete, absolute fixed frame");
             expect(result.at("status") == "state_matched" && result.at("command_accepted") == true && result.at("was_on") == false &&
                        result.at("matches_requested") == true && result.at("readback_safe") == true && result.at("physical_output_verified") == false &&
                        result.at("automatic_resend") == false && shelly_sets(fixture) == 1,
                    "previous state and acknowledgement must remain distinct from readback and physical output");
             for (const auto &transport_request : fixture.http.requests)
             {
                 expect(transport_request.digest_password ? transport_request.digest_realm == SHELLY_ID : !transport_request.digest_realm,
                        "every credentialed preflight, action and readback request must carry the same pinned realm");
             }
             no_device_tables(fixture);
         }},
        {"Shelly argument scope and binding are mandatory",
         []()
         {
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             auto arguments = shelly_output(fixture);
             arguments.erase("target");
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_switch_set", arguments);
                 },
                 "devices_arguments");
             arguments = shelly_output(fixture);
             arguments["switch_id"] = 1;
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_switch_set", arguments);
                 },
                 "devices_arguments");
             arguments = shelly_output(fixture);
             arguments["toggle_after"] = 5;
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_switch_set", arguments);
                 },
                 "devices_arguments");
             arguments = shelly_output(fixture);
             arguments["on"] = "toggle";
             fails(
                 [&]()
                 {
                     (void)fixture.call("shelly_switch_set", arguments);
                 },
                 "devices_arguments");
             expect(fixture.http.requests.empty(), "invalid action arguments must fail without transport");
         }},
        {"Shelly stale endpoint credential and safety bindings reject",
         []()
         {
             Fixture old({{"shelly", shelly_settings()}}, {"shelly"});
             const auto arguments = shelly_output(old);
             for (const int mode : {0, 1, 2, 3})
             {
                 auto settings = shelly_settings();
                 if (mode == 0)
                 {
                     settings["devices"]["lamp"]["base_url"] = "http://192.168.50.41";
                 }
                 else if (mode == 1)
                 {
                     settings["devices"]["lamp"]["password"] = "env:ANOTHER_SYNTHETIC_DEVICE_PASSWORD";
                 }
                 else if (mode == 2)
                 {
                     settings["devices"]["lamp"]["controls"]["0"]["max_power_watts"] = 59;
                 }
                 else
                 {
                     settings["timeout_seconds"] = 6;
                 }
                 Fixture replacement({{"shelly", settings}}, {"shelly"});
                 fails(
                     [&]()
                     {
                         (void)replacement.call("shelly_switch_set", arguments);
                     },
                     "devices_arguments");
                 expect(replacement.http.requests.empty(), "every changed approval-relevant setting must invalidate stale bindings");
             }
         }},
        {"Shelly matching was_on is not command confirmation",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             shelly_action(fixture, shelly_status(false), {{"was_on", true}});
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("command_accepted") == true && result.at("was_on") == true && result.at("matches_requested") == false &&
                        result.at("status") == "state_mismatch",
                    "was_on is the previous state even when it happens to equal the desired output");
         }},
        {"Shelly malformed acceptance cannot be rescued by matching readback",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             shelly_action(fixture, shelly_status(true), {{"was_on", "false"}});
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("command_accepted").is_null() && result.at("matches_requested") == true && result.at("status") == "uncertain" &&
                        result.at("command_error") == "devices_protocol" && shelly_sets(fixture) == 1,
                    "unknown command acceptance remains unknown");
         }},
        {"Shelly provider rejection preserves uncertainty without replay",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             shelly_action(fixture, shelly_status(true));
             fixture.http.routes.at(3).response.body =
                 Json({{"id", 3}, {"src", SHELLY_ID}, {"error", {{"code", -105}, {"message", "provider-private-details"}}}}).dump();
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("command_accepted").is_null() && result.at("matches_requested") == true && result.at("status") == "uncertain" &&
                        result.at("command_error") == "devices_provider" && result.dump().find("provider-private-details") == std::string::npos &&
                        shelly_sets(fixture) == 1,
                    "provider error text must never leak or cause an automatic write retry");
         }},
        {"Shelly ambiguous transport and unavailable readback are separate",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             identify(fixture);
             protected_status(fixture, 2, shelly_status());
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("command_error") == "devices_transport" && result.at("readback_error") == "devices_transport" &&
                        result.at("observation").is_null() && result.at("status") == "uncertain" && fixture.http.requests.size() == 5 &&
                        shelly_sets(fixture) == 1,
                    "a missing write reply must never be automatically replayed");
         }},
        {"Shelly accepted command with unavailable readback is unconfirmed",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             identify(fixture);
             protected_status(fixture, 2, shelly_status());
             fixture.http.add("POST", SHELLY + "/rpc", frame(3, {{"was_on", false}}));
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("command_accepted") == true && result.at("status") == "unconfirmed" && result.at("matches_requested").is_null() &&
                        result.at("observation").is_null(),
                    "preflight data cannot stand in for a missing readback");
         }},
        {"Shelly unsafe matching readback is not success or automatic correction",
         []()
         {
             PasswordEnvironment environment;
             Fixture fixture({{"shelly", shelly_settings()}}, {"shelly"});
             shelly_action(fixture, shelly_status(true, 61));
             const auto result = fixture.call("shelly_switch_set", shelly_output(fixture));
             expect(result.at("matches_requested") == true && result.at("readback_safe") == false && result.at("status") == "unsafe_readback" &&
                        shelly_sets(fixture) == 1,
                    "unsafe post-write metering must be reported without an unapproved corrective write");
         }},
        {"Shelly unknown provider status strings are minimized",
         []()
         {
             Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
             identify(fixture, false);
             auto raw = shelly_status();
             raw["errors"] = Json::array({"overtemp", "provider-private-details"});
             raw["source"] = "provider-private-details";
             fixture.http.add("POST", SHELLY + "/rpc", frame(2, raw));
             const auto result = fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
             expect(result.at("errors") == Json::array({"overtemp", "unrecognized_error"}) &&
                        result.dump().find("provider-private-details") == std::string::npos,
                    "only bounded recognized component errors may survive minimization");
         }},
        {"Shelly redirects status errors and oversized responses fail closed",
         []()
         {
             for (const long status : {302L, 500L, 429L})
             {
                 Fixture fixture({{"shelly", shelly_settings(false, false)}}, {"shelly"});
                 fixture.http.routes.push_back({"POST", SHELLY + "/rpc", {status, {}, "provider-private-details"}});
                 fails(
                     [&]()
                     {
                         (void)fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                     },
                     status == 302 ? "devices_redirect" : "devices_http");
                 expect(fixture.http.requests.size() == 1, "web failures must not be retried");
             }
             Fixture oversized({{"shelly", shelly_settings(false, false)}}, {"shelly"});
             oversized.http.routes.push_back({"POST", SHELLY + "/rpc", {200, {}, std::string(65537, 'x')}});
             fails(
                 [&]()
                 {
                     (void)oversized.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}});
                 },
                 "devices_protocol");
         }},
        {"Shelly reviewed Plus Plug US uses its exact pinned model",
         []()
         {
             auto settings = shelly_settings(false, false);
             settings["devices"]["lamp"]["expected_model"] = "SNPL-00116US";
             settings["devices"]["lamp"]["expected_device_id"] = "shellyplusplugus-000000000001";
             Fixture fixture({{"shelly", settings}}, {"shelly"});
             Json identity = {{"id", "shellyplusplugus-000000000001"}, {"model", "SNPL-00116US"}, {"gen", 2}, {"auth_en", false}};
             auto identity_frame = frame(1, identity);
             identity_frame["src"] = "shellyplusplugus-000000000001";
             auto status_frame = frame(2, shelly_status());
             status_frame["src"] = "shellyplusplugus-000000000001";
             fixture.http.add("POST", SHELLY + "/rpc", identity_frame);
             fixture.http.add("POST", SHELLY + "/rpc", status_frame);
             expect(fixture.call("shelly_status", {{"device", "lamp"}, {"switch_id", 0}}).at("output") == false,
                    "the second reviewed model must use the same fixed component protocol without inventing model equivalence");
         }},
        {"schema and summaries preserve exact approval values", []()
         {
             const Json preset = {{"seg", Json::array({{{"id", 0}, {"on", false}}})}};
             auto light = preset_settings(preset);
             light["devices"][0]["allow_global"] = true;
             Fixture fixture({{"wled", light}, {"shelly", shelly_settings()}}, {"wled", "shelly"});
             for (const auto &[name, tool] : fixture.registry.tools())
             {
                 (void)name;
                 check_schema(tool.schema);
                 expect(tool.schema.at("additionalProperties") == false, "every device tool must reject unknown fields");
                 if (tool.tier == Tier::act)
                 {
                     const auto &required = tool.schema.at("required");
                     expect(std::find(required.begin(), required.end(), tool.name.starts_with("wled_") ? Json("target_digest") : Json("target")) !=
                                required.end(),
                            "every action schema requires its binding");
                 }
             }
             const auto light_summary = fixture.registry.find("wled_set_power")->summary(wled_power(fixture));
             const auto plug_summary = fixture.registry.find("shelly_switch_set")->summary(shelly_output(fixture));
             expect(light_summary.find("false") != std::string::npos && light_summary.find("segment 0") != std::string::npos &&
                        plug_summary.find("ON") != std::string::npos && plug_summary.find("60 W") != std::string::npos,
                    "approval summaries must preserve absolute values, scope and safe-load limit");
         }}};
}
}  // namespace
}  // namespace switchboard::test

int main()
{
    const auto cases = switchboard::test::authored_cases();
    std::size_t failures = 0;
    for (const auto &[name, run] : cases)
    {
        try
        {
            run();
        }
        catch (const std::exception &error)
        {
            ++failures;
            std::cerr << "FAIL " << name << ": " << error.what() << '\n';
        }
    }
    std::cout << cases.size() << " native device cases, " << failures << " failures\n";
    return failures == 0 ? 0 : 1;
}

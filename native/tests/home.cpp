#include "support.hpp"

#include <algorithm>
#include <cstdlib>
#include <filesystem>
#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace switchboard::test
{
namespace
{
// Authored UNRUN. These fixtures never use accounts or real transports.
constexpr const char *TOKEN_NAME = "SWITCHBOARD_NATIVE_HOME_TEST_TOKEN";
constexpr const char *BASE_URL = "https://ha.example";

class HomeDirectory
{
public:
    HomeDirectory()
    {
        const auto token = std::chrono::steady_clock::now().time_since_epoch().count();
        path = std::filesystem::current_path() / (".native-home-test-" + std::to_string(getpid()) + "-" + std::to_string(token));
        if (!std::filesystem::create_directory(path) || chmod(path.c_str(), 0700) != 0)
        {
            throw std::runtime_error("cannot create private home test fixture");
        }
    }

    ~HomeDirectory()
    {
        std::error_code error;
        std::filesystem::remove_all(path, error);
        if (error)
        {
            std::cerr << "ERROR: home test fixture cleanup failed\n";
        }
    }

    HomeDirectory(const HomeDirectory &) = delete;
    HomeDirectory &operator=(const HomeDirectory &) = delete;
    std::filesystem::path path;
};

class Environment
{
public:
    Environment(std::string name, const std::string &value) : name_(std::move(name))
    {
        const char *previous = std::getenv(name_.c_str());
        if (previous != nullptr)
        {
            previous_ = previous;
        }
        if (setenv(name_.c_str(), value.c_str(), 1) != 0)
        {
            throw std::runtime_error("cannot set fake test credential");
        }
    }

    ~Environment()
    {
        if (previous_)
        {
            static_cast<void>(setenv(name_.c_str(), previous_->c_str(), 1));
        }
        else
        {
            static_cast<void>(unsetenv(name_.c_str()));
        }
    }

    Environment(const Environment &) = delete;
    Environment &operator=(const Environment &) = delete;

private:
    std::string name_;
    std::optional<std::string> previous_;
};

class HomeHttp final : public HttpTransport
{
public:
    FakeHttp fake;
    bool throw_post = false;

    HttpResponse request(const HttpRequest &input) override
    {
        if (throw_post && input.method == "POST")
        {
            fake.requests.push_back(input);
            throw Error("fake_http", "untrusted exception text containing fake-private-value");
        }
        return fake.request(input);
    }
};

Json home_settings()
{
    return {
        {"base_url", BASE_URL},
        {"token", std::string("env:") + TOKEN_NAME},
        {"expose", {"light.a", "light.b", "climate.room", "fan.room", "media_player.room", "sensor.room"}},
        {"lights", {"light.a", "light.b"}},
        {"rooms", {{"living", {"light.a", "climate.room", "fan.room", "media_player.room", "sensor.room"}}, {"study", {"light.b"}}}},
        {"climate",
         {{"climate.room", {{"unit", "C"}, {"minimum", 16}, {"maximum", 26}, {"max_change", 2}, {"modes", {"off", "heat", "cool", "heat_cool", "fan_only"}}}}}},
        {"fans", {{"fan.room", {{"minimum", 0}, {"maximum", 75}, {"max_change", 25}}}}},
        {"media_players", {{"media_player.room", {{"minimum", 0}, {"maximum", 0.4}}}}}};
}

Json home_configuration(const HomeDirectory &directory, const Json &settings = home_settings(), const bool enabled = true)
{
    return {{"switchboard", {{"connectors", enabled ? Json::array({"home"}) : Json::array()}, {"data_dir", directory.path.string()}}},
            {"connectors", {{"home", settings}}},
            {"policy", {{"tools", Json::object()}}},
            {"mcp", {{"expose", {"*"}}}}};
}

struct Fixture
{
    HomeDirectory directory;
    Json config;
    Database database;
    HomeHttp http;
    Context context;
    Registry registry;

    explicit Fixture(const Json &settings = home_settings(), const bool enabled = true)
        : config(home_configuration(directory, settings, enabled)), database(directory.path / "state.sqlite3"), context(config, directory.path, database, http,
                                                                                                                        []()
                                                                                                                        {
                                                                                                                            return 1794433200LL;
                                                                                                                        })
    {
        register_home(context, registry);
    }

    [[nodiscard]] const Tool &tool(const std::string &name) const
    {
        const auto *result = registry.find(name);
        expect(result != nullptr, "expected registered home tool");
        return *result;
    }

    [[nodiscard]] Json call(const std::string &name, const Json &arguments = Json::object()) const
    {
        return tool(name).handler(arguments);
    }

    [[nodiscard]] std::string binding() const
    {
        return call("home_targets").at("binding").get<std::string>();
    }

    [[nodiscard]] Json bound(Json arguments) const
    {
        arguments["binding"] = binding();
        return arguments;
    }

    void state_route(const Json &state)
    {
        http.fake.add("GET", std::string(BASE_URL) + "/api/states/" + state.at("entity_id").get<std::string>(), state);
    }

    void service_route(const std::string &service, const long status = 200)
    {
        http.fake.add("POST", std::string(BASE_URL) + "/api/services/" + service, Json::array(), status);
    }
};

template <typename Callable> void expect_home_error(Callable &&callable, const std::string &message)
{
    bool failed = false;
    try
    {
        callable();
    }
    catch (const Error &)
    {
        failed = true;
    }
    expect(failed, message);
}

Json raw_state(const std::string &identifier, const std::string &state, Json attributes = Json::object())
{
    return {{"entity_id", identifier},
            {"state", state},
            {"attributes", std::move(attributes)},
            {"last_changed", "2026-11-11T21:40:00+00:00"},
            {"last_updated", "2026-11-11T21:40:00+00:00"}};
}

Json light(const std::string &identifier = "light.a", const std::string &state = "on")
{
    return raw_state(identifier, state,
                     {{"supported_color_modes", {"rgb", "color_temp"}},
                      {"brightness", 128},
                      {"rgb_color", {255, 128, 0}},
                      {"min_color_temp_kelvin", 2000},
                      {"max_color_temp_kelvin", 6500},
                      {"color_temp_kelvin", 3000},
                      {"color_mode", "rgb"}});
}

Json climate(const std::string &mode = "heat")
{
    return raw_state("climate.room", mode,
                     {{"temperature_unit", "\xc2\xb0"
                                           "C"},
                      {"min_temp", 10},
                      {"max_temp", 30},
                      {"temperature", 20},
                      {"target_temp_low", 19},
                      {"target_temp_high", 23},
                      {"supported_features", 3},
                      {"hvac_modes", {"off", "heat", "cool", "heat_cool", "fan_only"}}});
}

Json fan(const int percentage = 40)
{
    return raw_state("fan.room", percentage == 0 ? "off" : "on", {{"percentage", percentage}, {"percentage_step", 1}, {"supported_features", 1}});
}

Json media(const std::string &state = "paused", const double volume = 0.3)
{
    return raw_state("media_player.room", state,
                     {{"volume_level", volume}, {"is_volume_muted", false}, {"supported_features", 20493}, {"group_members", {"media_player.room"}}});
}

void check_post(const Fixture &fixture, const std::size_t index, const std::string &service, const Json &body)
{
    const auto &request = fixture.http.fake.requests.at(index);
    expect(request.method == "POST" && request.url == std::string(BASE_URL) + "/api/services/" + service, "wrong home service method or endpoint");
    expect(Json::parse(request.body) == body, "home must submit only the exact reviewed body");
    expect(request.headers == std::map<std::string, std::string>{{"Accept", "application/json"},
                                                                 {"Authorization", "Bearer native-home-fake-token"},
                                                                 {"Content-Type", "application/json"}},
           "wrong action headers");
    expect(request.max_bytes == 65536 && request.timeout_seconds == 15 && !request.allow_http && !request.digest_password,
           "wrong bounded action transport options");
}

void check_observed(const Json &result)
{
    expect(result.at("request_status") == "accepted" && result.at("status") == "observed" && result.at("confirmed") == true,
           "expected independently observed requested state");
    expect(result.at("physical_delivery_verified") == false && result.at("retry_safe") == false, "readback must never claim physical delivery or retry safety");
}

std::size_t post_count(const Fixture &fixture)
{
    return static_cast<std::size_t>(std::count_if(fixture.http.fake.requests.begin(), fixture.http.fake.requests.end(),
                                                  [](const HttpRequest &request)
                                                  {
                                                      return request.method == "POST";
                                                  }));
}

void test_disabled_and_offline_registration()
{
    Fixture disabled(Json::object(), false);
    expect(disabled.registry.tools().empty() && disabled.http.fake.requests.empty(), "disabled home must not inspect settings or register tools");
    auto settings = home_settings();
    settings["token"] = "env:SWITCHBOARD_NATIVE_HOME_INTENTIONALLY_UNRESOLVED";
    Fixture fixture(settings);
    expect(fixture.registry.tools().size() == 7, "expected exactly seven native home tools");
    for (const auto &name : {"home_targets", "home_states", "home_capabilities"})
    {
        expect(fixture.tool(name).tier == Tier::read, "read tool tier changed");
    }
    for (const auto &name : {"home_lights", "home_climate", "home_fan", "home_media"})
    {
        expect(fixture.tool(name).tier == Tier::act, "device effects must retain the act tier");
    }
    const auto targets = fixture.call("home_targets");
    expect(targets.at("binding").get<std::string>().size() == 64 && !targets.contains("token") && !targets.contains("base_url"),
           "offline targets must not expose credentials or addresses");
    expect(fixture.http.fake.requests.empty() && fixture.context.jobs.empty(), "registration and targets must do no provider work and add no jobs");
    expect(fixture.database.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name GLOB 'native_home_*'").empty(),
           "the stateless native adapter must not create home tables");
    for (const auto &name : {"home_call_service", "home_poll", "home_poll_status", "home_sensor_summary", "home_announce", "home_lighting_preset"})
    {
        expect(fixture.registry.find(name) == nullptr, "unsupported tool must not be a placeholder");
    }
    Fixture empty({{"base_url", BASE_URL}, {"token", "env:SWITCHBOARD_NATIVE_HOME_INTENTIONALLY_UNRESOLVED"}});
    expect(empty.registry.tools().size() == 7, "empty allowlists must register valid, unusable action schemas");
    expect_home_error(
        [&]()
        {
            static_cast<void>(empty.call("home_fan", empty.bound({{"entity_id", "fan.room"}, {"percentage", 10}})));
        },
        "an empty profile must fail before approval or provider work");
    expect(empty.http.fake.requests.empty(), "empty allowlists must not contact the provider");
}

void test_settings_reject_unsupported_and_unsafe_scope()
{
    for (const auto &key : {"poll", "speech", "quiet_hours", "lighting_presets", "timezone", "retry"})
    {
        auto settings = home_settings();
        settings[key] = {{"enabled", false}};
        expect_home_error(
            [&]()
            {
                Fixture fixture(settings);
            },
            "unsupported settings must fail even when disabled");
    }
    const std::vector<std::function<void(Json &)>> corruptions{[](Json &settings)
                                                               {
                                                                   settings["token"] = "raw-token";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["token"] = "cmd:echo";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["token"] = "env:bad-name";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["token"] = "env:" + std::string(129, 'A');
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["token"] = "file:";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["allow_http"] = 1;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["base_url"] = "http://ha.example";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["base_url"] = "https://user:pass@ha.example";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["base_url"] = "https://ha.example/api";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["base_url"] = "https://ha.example?query=1";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["base_url"] = "https://ha.example:0";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("light.*");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("light.a");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("lock.front");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("cover.garage");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("alarm_control_panel.house");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("switch.heater");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("script.house");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["expose"].push_back("camera.front");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["lights"].push_back("light.hidden");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["deny_domains"] = {"light"};
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["rooms"]["living"].push_back("light.hidden");
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["rooms"]["bad room"] = {"light.a"};
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["rooms"]["empty"] = Json::array();
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["fans"]["fan.room"]["minimum"] = true;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["fans"]["fan.room"]["maximum"] = 101;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["fans"]["fan.room"]["max_change"] = 0;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["fans"]["fan.room"]["preset"] = "eco";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["fans"]["light.a"] = Json::object();
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["unit"] = "K";
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["minimum"] = 30;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["maximum"] = true;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["modes"] = {"auto"};
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["modes"] = {"off", "off"};
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["modes"] = Json::array();
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["climate"]["climate.room"]["max_change"] = 0;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["media_players"]["media_player.room"]["maximum"] = 1.1;
                                                               },
                                                               [](Json &settings)
                                                               {
                                                                   settings["media_players"]["media_player.room"]["extra"] = false;
                                                               }};
    for (const auto &corrupt : corruptions)
    {
        auto settings = home_settings();
        corrupt(settings);
        expect_home_error(
            [&]()
            {
                Fixture fixture(settings);
            },
            "unsafe native home configuration accepted");
    }
}

void test_canonical_binding_and_reload()
{
    Fixture original;
    auto equivalent = home_settings();
    equivalent["base_url"] = "HTTPS://HA.EXAMPLE:443/";
    equivalent["token"] = "file:/nonexistent/native-home-fake-token";
    equivalent["allow_http"] = false;
    equivalent["deny_domains"] = Json::array();
    std::reverse(equivalent["expose"].begin(), equivalent["expose"].end());
    std::reverse(equivalent["lights"].begin(), equivalent["lights"].end());
    std::reverse(equivalent["rooms"]["living"].begin(), equivalent["rooms"]["living"].end());
    std::reverse(equivalent["climate"]["climate.room"]["modes"].begin(), equivalent["climate"]["climate.room"]["modes"].end());
    equivalent["climate"]["climate.room"]["minimum"] = 16.0;
    equivalent["media_players"]["media_player.room"]["minimum"] = -0.0;
    Fixture normalized(equivalent);
    expect(original.binding() == normalized.binding(), "binding must normalize sets, defaults, numbers and origin");
    const auto original_binding = original.binding();
    const std::vector<std::function<void(Json &)>> changes{[](Json &settings)
                                                           {
                                                               settings["base_url"] = "https://other.example";
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["allow_http"] = true;
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["expose"].push_back("sensor.other");
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["deny_domains"] = {"binary_sensor"};
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["lights"] = {"light.a"};
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["rooms"]["living"].push_back("light.b");
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["climate"]["climate.room"]["maximum"] = 25;
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["fans"]["fan.room"]["max_change"] = 20;
                                                           },
                                                           [](Json &settings)
                                                           {
                                                               settings["media_players"]["media_player.room"]["maximum"] = 0.35;
                                                           }};
    for (const auto &change : changes)
    {
        auto settings = home_settings();
        change(settings);
        Fixture changed(settings);
        expect(changed.binding() != original_binding, "a reviewed configuration change must invalidate binding");
        const Json old_arguments = {{"binding", original_binding}, {"entity_id", "fan.room"}, {"percentage", 50}};
        expect_home_error(
            [&]()
            {
                validate(old_arguments, changed.tool("home_fan").schema);
            },
            "reloaded schema must reject queued stale binding");
        expect_home_error(
            [&]()
            {
                static_cast<void>(changed.call("home_fan", old_arguments));
            },
            "handler must also reject stale binding");
        expect(changed.http.fake.requests.empty(), "stale binding must fail before secret or network access");
    }
}

void test_schemas_bind_targets_and_bound_shapes()
{
    Fixture fixture;
    const auto &light_schema = fixture.tool("home_lights").schema;
    expect(light_schema.at("properties").at("binding").at("enum") == Json::array({fixture.binding()}),
           "action catalog must pin the current configuration binding");
    const auto &entities = light_schema.at("properties").at("entity_ids").at("items").at("enum");
    expect(entities == Json::array({"light.a", "light.b"}), "schema must enumerate only reviewed light targets");
    std::vector<Json> light_arguments{{{"entity_ids", {"light.a"}}, {"state", "on"}, {"brightness_pct", 0}},
                                      {{"entity_ids", {"light.a"}}, {"state", "on"}, {"brightness_pct", 101}},
                                      {{"entity_ids", {"light.a"}}, {"state", "on"}, {"rgb_color", {0, 0}}},
                                      {{"entity_ids", {"light.a"}}, {"state", "on"}, {"rgb_color", {0, true, 0}}},
                                      {{"entity_ids", {"light.a"}}, {"state", "on"}, {"color_temp_kelvin", 999}},
                                      {{"entity_ids", {"light.a"}}, {"state", "on"}, {"color_temp_kelvin", 20001}},
                                      {{"entity_ids", {"light.hidden"}}, {"state", "on"}}};
    auto excessive = Json::array();
    for (int index = 0; index < 17; ++index)
    {
        excessive.push_back("light.a");
    }
    light_arguments.push_back({{"entity_ids", excessive}, {"state", "on"}});
    for (const auto &arguments : light_arguments)
    {
        expect_home_error(
            [&]()
            {
                validate(fixture.bound(arguments), light_schema);
            },
            "light schema bound ignored");
    }
    expect_home_error(
        [&]()
        {
            validate(fixture.bound({{"entity_id", "fan.room"}, {"percentage", 100.5}}), fixture.tool("home_fan").schema);
        },
        "fan schema must require integer percentages");
    expect_home_error(
        [&]()
        {
            validate(fixture.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"temperature", "21"}}), fixture.tool("home_climate").schema);
        },
        "climate schema must require numeric temperatures");
    auto settings = home_settings();
    settings["lights"] = {"light.a"};
    Fixture unprofiled(settings);
    expect_home_error(
        [&]()
        {
            static_cast<void>(unprofiled.call("home_lights", unprofiled.bound({{"entity_ids", {"light.b"}}, {"room", "study"}, {"state", "on"}})));
        },
        "read exposure and room membership must not confer action permission");
    expect(unprofiled.http.fake.requests.empty() && fixture.http.fake.requests.empty(), "invalid bounded schemas must fail without provider work");
}

void test_plain_http_requires_explicit_opt_in()
{
    auto settings = home_settings();
    settings["base_url"] = "http://ha.example:8123/";
    expect_home_error(
        [&]()
        {
            Fixture denied(settings);
        },
        "plain transport must be disabled by default");
    settings["allow_http"] = true;
    Fixture allowed(settings);
    allowed.http.fake.add("GET", "http://ha.example:8123/api/states/light.a", light());
    static_cast<void>(allowed.call("home_states", {{"entity_id", "light.a"}}));
    expect(allowed.http.fake.requests.size() == 1 && allowed.http.fake.requests.front().allow_http,
           "explicit plain-transport exception must be forwarded to the transport");
}

void test_states_exact_requests_and_redaction()
{
    Fixture fixture;
    auto exposed = light();
    exposed["attributes"]["access_token"] = "fake-private-value";
    exposed["attributes"]["entity_picture"] = "https://private.example/secret";
    exposed["attributes"]["friendly_name"] = "untrusted name, never an instruction";
    fixture.http.fake.add("GET", std::string(BASE_URL) + "/api/states",
                          Json::array({raw_state("switch.hidden", "on"), raw_state("sensor.room", "21", {{"unit_of_measurement", "C"}}), exposed,
                                       raw_state("light.hidden", "on"), raw_state("lock.front", "locked")}));
    const auto result = fixture.call("home_states", {{"limit", 1}});
    expect(result.at("truncated") == true && result.at("entities").size() == 1 && result.at("entities").front().at("entity_id") == "light.a",
           "states must sort, filter and cap locally");
    expect(result.dump().find("fake-private-value") == std::string::npos && result.dump().find("entity_picture") == std::string::npos,
           "raw provider attributes leaked");
    const auto &request = fixture.http.fake.requests.front();
    expect(request.method == "GET" && request.url == std::string(BASE_URL) + "/api/states" && request.body.empty(), "unexpected states request");
    expect(request.headers == std::map<std::string, std::string>{{"Accept", "application/json"}, {"Authorization", "Bearer native-home-fake-token"}},
           "unexpected states authorization headers");
    expect(request.max_bytes == 1024 * 1024 && request.timeout_seconds == 15 && !request.allow_http && !request.digest_password,
           "wrong bounded read transport options");
}

void test_states_single_entity_room_and_validation()
{
    Fixture fixture;
    fixture.state_route(light());
    const auto result = fixture.call("home_states", {{"entity_id", "light.a"}, {"room", "living"}, {"domain", "light"}});
    expect(result.at("entities").size() == 1 && result.at("truncated") == false, "single exact state read must not expand the room");
    expect(fixture.http.fake.requests.size() == 1, "room-scoped read must fetch only the explicit entity");
    for (const auto &arguments : std::vector<Json>{{{"entity_id", "light.b"}, {"room", "living"}},
                                                   {{"entity_id", "light.hidden"}},
                                                   {{"entity_id", "light.a"}, {"domain", "fan"}},
                                                   {{"room", "missing"}},
                                                   {{"entity_id", "light.a/../lock.front"}},
                                                   {{"limit", 201}},
                                                   {{"limit", true}},
                                                   {{"extra", false}}})
    {
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_states", arguments));
            },
            "invalid read scope or shape must fail");
    }
    expect(fixture.http.fake.requests.size() == 1, "invalid state queries must not contact the provider");
}

void test_states_malformed_duplicate_and_capped_responses()
{
    for (const auto &response : std::vector<Json>{Json::object(), Json::array({light(), light()}),
                                                  Json::array({{{"entity_id", "light.a"}, {"state", true}, {"attributes", Json::object()}}}),
                                                  Json::array({{{"entity_id", "light.a/evil"}, {"state", "on"}, {"attributes", Json::object()}}})})
    {
        Fixture fixture;
        fixture.http.fake.add("GET", std::string(BASE_URL) + "/api/states", response);
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_states"));
            },
            "malformed state list accepted");
    }
    Fixture oversized;
    oversized.http.fake.routes.push_back({"GET", std::string(BASE_URL) + "/api/states", {200, {}, std::string(1024 * 1024 + 1, ' ')}});
    expect_home_error(
        [&]()
        {
            static_cast<void>(oversized.call("home_states"));
        },
        "state response cap bypassed");
    Fixture refused;
    refused.http.fake.add("GET", std::string(BASE_URL) + "/api/states", {{"error", "fake-private-value"}}, 401);
    try
    {
        static_cast<void>(refused.call("home_states"));
        expect(false, "refused state response accepted");
    }
    catch (const Error &error)
    {
        expect(std::string(error.what()).find("fake-private-value") == std::string::npos, "provider refusal body leaked into error");
    }
}

void test_capabilities_are_live_and_advisory()
{
    Fixture fixture;
    fixture.state_route(climate());
    const auto result = fixture.call("home_capabilities", {{"entity_id", "climate.room"}, {"room", "living"}});
    expect(result.at("binding") == fixture.binding() && result.at("profiled") == true && result.at("profile").at("unit") == "C",
           "capabilities must include the current reviewed profile");
    expect(result.at("reported_support").at("temperature") == true && result.at("reported_support").at("target_range") == true,
           "reported feature decoding changed");
    auto unavailable = light("light.a", "unavailable");
    fixture.state_route(unavailable);
    const auto unavailable_result = fixture.call("home_capabilities", {{"entity_id", "light.a"}});
    expect(unavailable_result.at("observation").at("availability") == "unavailable", "capability reporting must not invent availability");
    fixture.state_route(raw_state("sensor.room", "21"));
    const auto sensor = fixture.call("home_capabilities", {{"entity_id", "sensor.room"}});
    expect(sensor.at("profiled") == false && sensor.at("reported_support").empty(), "sensor names must not imply writable capabilities");
    expect(fixture.http.fake.requests.size() == 3 && post_count(fixture) == 0, "capability reads must never issue an action");
}

void test_light_exact_request_and_observed_readback()
{
    Fixture fixture;
    fixture.state_route(light("light.a", "off"));
    fixture.service_route("light/turn_on");
    fixture.state_route(light());
    const auto result = fixture.call(
        "home_lights", fixture.bound({{"entity_ids", {"light.a"}}, {"room", "living"}, {"state", "on"}, {"brightness_pct", 50}, {"rgb_color", {255, 128, 0}}}));
    check_observed(result);
    check_post(fixture, 1, "light/turn_on", {{"entity_id", "light.a"}, {"brightness_pct", 50}, {"rgb_color", {255, 128, 0}}});
    expect(fixture.http.fake.requests.size() == 3, "one light action must preflight, submit once and read back once");
    Fixture kelvin;
    kelvin.state_route(light());
    kelvin.service_route("light/turn_on");
    auto after = light();
    after["attributes"]["color_mode"] = "color_temp";
    kelvin.state_route(after);
    check_observed(kelvin.call("home_lights", kelvin.bound({{"entity_ids", {"light.a"}}, {"state", "on"}, {"color_temp_kelvin", 3000}})));
    check_post(kelvin, 1, "light/turn_on", {{"entity_id", "light.a"}, {"color_temp_kelvin", 3000}});
}

void test_multi_light_preflight_and_concrete_room_scope()
{
    Fixture fixture;
    fixture.state_route(light("light.b"));
    fixture.state_route(light());
    fixture.service_route("light/turn_off");
    fixture.state_route(light("light.b", "off"));
    fixture.state_route(light("light.a", "off"));
    const auto result = fixture.call("home_lights", fixture.bound({{"entity_ids", {"light.b", "light.a"}}, {"state", "off"}}));
    check_observed(result);
    check_post(fixture, 2, "light/turn_off", {{"entity_id", {"light.b", "light.a"}}});
    expect(fixture.http.fake.requests.size() == 5 && result.at("observations").size() == 2,
           "multi-light operation must preflight every explicit target before its one submission");
    Fixture unavailable;
    unavailable.state_route(light());
    unavailable.state_route(light("light.b", "unavailable"));
    expect_home_error(
        [&]()
        {
            static_cast<void>(unavailable.call("home_lights", unavailable.bound({{"entity_ids", {"light.a", "light.b"}}, {"state", "off"}})));
        },
        "late failed preflight must prevent all effects");
    expect(post_count(unavailable) == 0, "preflight failure must not partially submit multi-light effects");
    Fixture scoped;
    expect_home_error(
        [&]()
        {
            static_cast<void>(scoped.call("home_lights", scoped.bound({{"entity_ids", {"light.a", "light.b"}}, {"room", "living"}, {"state", "on"}})));
        },
        "room must only restrict, not expand or ignore targets");
    expect(scoped.http.fake.requests.empty(), "room mismatch must fail before any provider read");
}

void test_light_rejects_conflicts_groups_and_missing_capabilities()
{
    Fixture fixture;
    for (const auto &arguments : std::vector<Json>{{{"entity_ids", {"light.a", "light.a"}}, {"state", "on"}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "off"}, {"brightness_pct", 40}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "on"}, {"rgb_color", {1, 2, 3}}, {"color_temp_kelvin", 3000}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "on"}, {"transition", 0}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "on"}, {"area_id", "all"}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "on"}, {"brightness_pct", true}},
                                                   {{"entity_ids", {"light.a"}}, {"state", "on"}, {"rgb_color", {1, 2, 256}}},
                                                   {{"entity_ids", Json::array()}, {"state", "on"}}})
    {
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_lights", fixture.bound(arguments)));
            },
            "invalid lighting shape or conflict accepted");
    }
    expect(fixture.http.fake.requests.empty(), "invalid light arguments must fail offline");
    for (const auto &mutate : std::vector<std::function<void(Json &)>>{[](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_color_modes"] = {"onoff"};
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_color_modes"] = {"future_mode"};
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["entity_id"] = {"light.hidden"};
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["group_members"] = "light.hidden";
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["state"] = "unknown";
                                                                       }})
    {
        Fixture denied;
        auto before = light();
        mutate(before);
        denied.state_route(before);
        expect_home_error(
            [&]()
            {
                static_cast<void>(denied.call("home_lights", denied.bound({{"entity_ids", {"light.a"}}, {"state", "on"}, {"brightness_pct", 40}})));
            },
            "unsupported or grouped light accepted");
        expect(post_count(denied) == 0, "failed light capability must prevent submission");
    }
    Fixture kelvin;
    kelvin.state_route(light());
    expect_home_error(
        [&]()
        {
            static_cast<void>(kelvin.call("home_lights", kelvin.bound({{"entity_ids", {"light.a"}}, {"state", "on"}, {"color_temp_kelvin", 7000}})));
        },
        "kelvin outside live bounds accepted");
    expect(post_count(kelvin) == 0, "invalid kelvin must prevent submission");
}

void test_climate_single_and_range_requests()
{
    Fixture fixture;
    fixture.state_route(climate());
    fixture.service_route("climate/set_temperature");
    auto single = climate();
    single["attributes"]["temperature"] = 21;
    fixture.state_route(single);
    check_observed(fixture.call("home_climate", fixture.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"temperature", 21}})));
    check_post(fixture, 1, "climate/set_temperature", {{"entity_id", "climate.room"}, {"temperature", 21}});
    fixture.state_route(single);
    fixture.service_route("climate/set_temperature");
    auto ranged = climate("heat_cool");
    ranged["attributes"]["target_temp_low"] = 20;
    ranged["attributes"]["target_temp_high"] = 24;
    fixture.state_route(ranged);
    check_observed(fixture.call(
        "home_climate",
        fixture.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"hvac_mode", "heat_cool"}, {"target_temp_low", 20}, {"target_temp_high", 24}})));
    check_post(fixture, 4, "climate/set_temperature",
               {{"entity_id", "climate.room"}, {"hvac_mode", "heat_cool"}, {"target_temp_low", 20}, {"target_temp_high", 24}});
}

void test_climate_modes_and_fahrenheit()
{
    Fixture off;
    auto before = climate();
    before["attributes"].erase("temperature_unit");
    before["attributes"].erase("temperature");
    off.state_route(before);
    off.service_route("climate/set_hvac_mode");
    before["state"] = "off";
    off.state_route(before);
    check_observed(off.call("home_climate", off.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"hvac_mode", "off"}})));
    check_post(off, 1, "climate/set_hvac_mode", {{"entity_id", "climate.room"}, {"hvac_mode", "off"}});
    Fixture heat;
    heat.state_route(climate("off"));
    heat.service_route("climate/set_hvac_mode");
    heat.state_route(climate());
    check_observed(heat.call("home_climate", heat.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"hvac_mode", "heat"}})));
    auto settings = home_settings();
    settings["climate"]["climate.room"]["unit"] = "F";
    settings["climate"]["climate.room"]["minimum"] = 60;
    settings["climate"]["climate.room"]["maximum"] = 78;
    Fixture fahrenheit(settings);
    auto raw = climate();
    raw["attributes"]["temperature_unit"] = "\xc2\xb0"
                                            "F";
    raw["attributes"]["min_temp"] = 50;
    raw["attributes"]["max_temp"] = 90;
    raw["attributes"]["temperature"] = 70;
    fahrenheit.state_route(raw);
    fahrenheit.service_route("climate/set_temperature");
    raw["attributes"]["temperature"] = 71;
    fahrenheit.state_route(raw);
    check_observed(fahrenheit.call("home_climate", fahrenheit.bound({{"entity_id", "climate.room"}, {"unit", "F"}, {"temperature", 71}})));
    check_post(fahrenheit, 1, "climate/set_temperature", {{"entity_id", "climate.room"}, {"temperature", 71}});
}

void test_climate_refuses_unit_bounds_caps_and_change()
{
    Fixture invalid;
    for (const auto &arguments :
         std::vector<Json>{{{"entity_id", "climate.room"}, {"unit", "F"}, {"temperature", 21}},
                           {{"entity_id", "climate.room"}, {"unit", "C"}},
                           {{"entity_id", "climate.room"}, {"unit", "C"}, {"target_temp_low", 19}},
                           {{"entity_id", "climate.room"}, {"unit", "C"}, {"target_temp_low", 24}, {"target_temp_high", 20}},
                           {{"entity_id", "climate.room"}, {"unit", "C"}, {"temperature", 21}, {"target_temp_low", 19}, {"target_temp_high", 24}},
                           {{"entity_id", "climate.room"}, {"unit", "C"}, {"hvac_mode", "auto"}}})
    {
        expect_home_error(
            [&]()
            {
                static_cast<void>(invalid.call("home_climate", invalid.bound(arguments)));
            },
            "invalid climate arguments accepted");
    }
    expect(invalid.http.fake.requests.empty(), "invalid climate combinations must fail before a provider read");
    for (const auto &mutate : std::vector<std::function<void(Json &)>>{[](Json &raw)
                                                                       {
                                                                           raw["attributes"]["temperature_unit"] = "\xc2\xb0"
                                                                                                                   "F";
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"].erase("temperature_unit");
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"].erase("min_temp");
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["max_temp"] = 20;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_features"] = 0;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_features"] = true;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["temperature"] = 16;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"].erase("temperature");
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["hvac_modes"] = {"off"};
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["state"] = "off";
                                                                       }})
    {
        Fixture fixture;
        auto raw = climate();
        mutate(raw);
        fixture.state_route(raw);
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_climate", fixture.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"temperature", 21}})));
            },
            "unsafe live climate state accepted");
        expect(post_count(fixture) == 0, "climate capability or bounds failure must prevent submission");
    }
    Fixture energize;
    auto unsafe = climate("off");
    unsafe["attributes"]["temperature"] = 30;
    energize.state_route(unsafe);
    expect_home_error(
        [&]()
        {
            static_cast<void>(energize.call("home_climate", energize.bound({{"entity_id", "climate.room"}, {"unit", "C"}, {"hvac_mode", "heat"}})));
        },
        "mode-only heating must not energize an out-of-profile existing target");
    expect(post_count(energize) == 0, "unsafe mode-only climate action submitted");
}

void test_fan_limits_change_and_zero()
{
    Fixture fixture;
    fixture.state_route(fan());
    fixture.service_route("fan/set_percentage");
    fixture.state_route(fan(60));
    check_observed(fixture.call("home_fan", fixture.bound({{"entity_id", "fan.room"}, {"room", "living"}, {"percentage", 60}})));
    check_post(fixture, 1, "fan/set_percentage", {{"entity_id", "fan.room"}, {"percentage", 60}});
    Fixture zero;
    zero.state_route(fan(20));
    zero.service_route("fan/set_percentage");
    zero.state_route(fan(0));
    check_observed(zero.call("home_fan", zero.bound({{"entity_id", "fan.room"}, {"percentage", 0}})));
    check_post(zero, 1, "fan/set_percentage", {{"entity_id", "fan.room"}, {"percentage", 0}});
}

void test_fan_rejects_unknown_percentage_and_features()
{
    Fixture over_limit;
    expect_home_error(
        [&]()
        {
            static_cast<void>(over_limit.call("home_fan", over_limit.bound({{"entity_id", "fan.room"}, {"percentage", 76}})));
        },
        "configured fan maximum ignored");
    expect(over_limit.http.fake.requests.empty(), "out-of-profile fan value must fail offline");
    for (const auto &mutate : std::vector<std::function<void(Json &)>>{[](Json &raw)
                                                                       {
                                                                           raw["attributes"].erase("percentage");
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["percentage"] = true;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["percentage"] = 101;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["percentage"] = 0;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_features"] = 0;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_features"] = true;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["group_members"] = {"fan.hidden"};
                                                                       }})
    {
        Fixture fixture;
        auto raw = fan();
        mutate(raw);
        fixture.state_route(raw);
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_fan", fixture.bound({{"entity_id", "fan.room"}, {"percentage", 60}})));
            },
            "unsafe fan preflight accepted");
        expect(post_count(fixture) == 0, "failed fan preflight must prevent submission");
    }
}

void test_media_actions_exact_requests()
{
    const std::vector<std::pair<Json, std::string>> actions{{{{"action", "play"}}, "media_play"},
                                                            {{{"action", "pause"}}, "media_pause"},
                                                            {{{"action", "stop"}}, "media_stop"},
                                                            {{{"action", "volume"}, {"volume_level", 0.35}}, "volume_set"},
                                                            {{{"action", "mute"}, {"is_volume_muted", true}}, "volume_mute"},
                                                            {{{"action", "mute"}, {"is_volume_muted", false}}, "volume_mute"}};
    for (const auto &[arguments, service] : actions)
    {
        Fixture fixture;
        fixture.state_route(media());
        fixture.service_route("media_player/" + service);
        auto after = media();
        Json body = {{"entity_id", "media_player.room"}};
        const auto action = arguments.at("action").get<std::string>();
        if (action == "volume")
        {
            body["volume_level"] = arguments.at("volume_level");
            after["attributes"]["volume_level"] = arguments.at("volume_level");
        }
        else if (action == "mute")
        {
            body["is_volume_muted"] = arguments.at("is_volume_muted");
            after["attributes"]["is_volume_muted"] = arguments.at("is_volume_muted");
        }
        else
        {
            after["state"] = action == "play" ? "playing" : (action == "pause" ? "paused" : "idle");
        }
        fixture.state_route(after);
        auto input = arguments;
        input["entity_id"] = "media_player.room";
        check_observed(fixture.call("home_media", fixture.bound(input)));
        check_post(fixture, 1, "media_player/" + service, body);
        expect(fixture.http.fake.requests.size() == 3 && post_count(fixture) == 1, "media action must not implicitly set volume, unmute or power on");
    }
}

void test_media_refuses_volume_group_features_and_values()
{
    Fixture invalid;
    for (const auto &arguments : std::vector<Json>{{{"action", "volume"}},
                                                   {{"action", "play"}, {"volume_level", 0.2}},
                                                   {{"action", "mute"}, {"is_volume_muted", 1}},
                                                   {{"action", "volume"}, {"volume_level", 0.41}},
                                                   {{"action", "mute"}, {"is_volume_muted", false}, {"volume_level", 0.2}},
                                                   {{"action", "play"}, {"media_content_id", "https://unreviewed.example/media"}},
                                                   {{"action", "join"}, {"group_members", {"media_player.hidden"}}}})
    {
        auto input = arguments;
        input["entity_id"] = "media_player.room";
        expect_home_error(
            [&]()
            {
                static_cast<void>(invalid.call("home_media", invalid.bound(input)));
            },
            "unsupported or unsafe media arguments accepted");
    }
    expect(invalid.http.fake.requests.empty(), "invalid media values must fail offline");
    for (const auto &mutate : std::vector<std::function<void(Json &)>>{[](Json &raw)
                                                                       {
                                                                           raw["attributes"]["volume_level"] = 0.6;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"].erase("volume_level");
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["volume_level"] = true;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["supported_features"] = 0;
                                                                       },
                                                                       [](Json &raw)
                                                                       {
                                                                           raw["attributes"]["group_members"] = {"media_player.room", "media_player.hidden"};
                                                                       }})
    {
        Fixture fixture;
        auto raw = media();
        mutate(raw);
        fixture.state_route(raw);
        expect_home_error(
            [&]()
            {
                static_cast<void>(fixture.call("home_media", fixture.bound({{"entity_id", "media_player.room"}, {"action", "play"}})));
            },
            "unsafe media play preflight accepted");
        expect(post_count(fixture) == 0, "media safety preflight must prevent submission");
    }
    Fixture mute;
    auto before = media();
    before["attributes"].erase("volume_level");
    mute.state_route(before);
    mute.service_route("media_player/volume_mute");
    before["attributes"]["is_volume_muted"] = true;
    mute.state_route(before);
    check_observed(mute.call("home_media", mute.bound({{"entity_id", "media_player.room"}, {"action", "mute"}, {"is_volume_muted", true}})));
}

void test_readback_mismatch_unavailable_and_profile_tolerance()
{
    for (const auto &after : std::vector<Json>{fan(59), raw_state("fan.room", "unavailable")})
    {
        Fixture fixture;
        fixture.state_route(fan());
        fixture.service_route("fan/set_percentage");
        fixture.state_route(after);
        const auto result = fixture.call("home_fan", fixture.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
        expect(result.at("request_status") == "accepted" && result.at("status") == "requested_unconfirmed" && result.at("confirmed") == false,
               "mismatched or unavailable readback falsely confirmed");
        expect(post_count(fixture) == 1, "readback mismatch must never resend an effect");
    }
    Fixture volume;
    volume.state_route(media());
    volume.service_route("media_player/volume_set");
    volume.state_route(media("paused", 0.405));
    const auto beyond = volume.call("home_media", volume.bound({{"entity_id", "media_player.room"}, {"action", "volume"}, {"volume_level", 0.4}}));
    expect(beyond.at("observations").front().at("matches_request") == true && beyond.at("observations").front().at("within_profile") == false &&
               beyond.at("confirmed") == false,
           "numeric tolerance must not loosen a configured bound");
    Fixture grouped;
    grouped.state_route(fan());
    grouped.service_route("fan/set_percentage");
    auto group = fan(60);
    group["attributes"]["group_members"] = {"fan.hidden"};
    grouped.state_route(group);
    const auto result = grouped.call("home_fan", grouped.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
    expect(result.at("confirmed") == false && result.at("observations").front().at("reported_leaf") == false,
           "newly reported group membership must invalidate confirmation");
    Fixture capability;
    capability.state_route(media());
    capability.service_route("media_player/media_pause");
    auto after = media();
    after["attributes"]["supported_features"] = 0;
    capability.state_route(after);
    const auto changed = capability.call("home_media", capability.bound({{"entity_id", "media_player.room"}, {"action", "pause"}}));
    expect(changed.at("confirmed") == false && changed.at("observations").front().at("within_profile") == false,
           "readback must retain the capability required by the submitted media action");
    Fixture color;
    color.state_route(light());
    color.service_route("light/turn_on");
    auto invalid_color = light();
    invalid_color["attributes"]["rgb_color"] = {256, 128, 0};
    color.state_route(invalid_color);
    const auto invalid = color.call("home_lights", color.bound({{"entity_ids", {"light.a"}}, {"state", "on"}, {"rgb_color", {255, 128, 0}}}));
    expect(invalid.at("confirmed") == false && invalid.at("observations").front().at("within_profile") == false,
           "color tolerance must not admit channels beyond their absolute bounds");
}

void test_submission_unknown_and_rejected_without_retry()
{
    for (const long status : {503L, 500L, 408L, 0L, 403L, 401L, 302L, 429L})
    {
        Fixture fixture;
        fixture.state_route(fan());
        fixture.service_route("fan/set_percentage", status);
        const bool rejected = status >= 300 && status < 500 && status != 408;
        if (!rejected)
        {
            fixture.state_route(fan(60));
        }
        const auto result = fixture.call("home_fan", fixture.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
        expect(result.at("request_status") == (rejected ? "rejected" : "unknown") && result.at("confirmed") == false,
               "ambiguous or refused submission falsely confirmed");
        expect(result.at("status") == (rejected ? "rejected" : "requested_unconfirmed") && fixture.http.fake.requests.size() == (rejected ? 2 : 3) &&
                   post_count(fixture) == 1,
               "redirects and refusals must not retry; ambiguity permits only one readback");
    }
    Fixture transport;
    transport.http.throw_post = true;
    transport.state_route(fan());
    transport.state_route(fan(60));
    const auto result = transport.call("home_fan", transport.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
    expect(result.at("request_status") == "unknown" && result.at("confirmed") == false && post_count(transport) == 1,
           "transport exception must remain ambiguous without resend");
    expect(result.dump().find("fake-private-value") == std::string::npos, "transport exception text leaked");
}

void test_post_body_is_not_confirmation()
{
    Fixture misleading;
    misleading.state_route(fan());
    misleading.http.fake.add("POST", std::string(BASE_URL) + "/api/services/fan/set_percentage", Json::array({fan(60)}));
    misleading.state_route(fan());
    const auto mismatch = misleading.call("home_fan", misleading.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
    expect(mismatch.at("confirmed") == false, "provider changed-state list must not replace independent readback");
    Fixture unreadable;
    unreadable.state_route(fan());
    unreadable.http.fake.routes.push_back({"POST", std::string(BASE_URL) + "/api/services/fan/set_percentage", {200, {}, "not JSON"}});
    unreadable.state_route(fan(60));
    check_observed(unreadable.call("home_fan", unreadable.bound({{"entity_id", "fan.room"}, {"percentage", 60}})));
    Fixture capped;
    capped.state_route(fan());
    capped.http.fake.routes.push_back({"POST", std::string(BASE_URL) + "/api/services/fan/set_percentage", {200, {}, std::string(65537, 'x')}});
    capped.state_route(fan(60));
    const auto result = capped.call("home_fan", capped.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
    expect(result.at("request_status") == "unknown" && result.at("confirmed") == false,
           "over-cap submission response must remain ambiguous even if readback matches");
}

void test_readback_failure_is_reported_without_resend()
{
    for (const auto &response : std::vector<HttpResponse>{{404, {}, "{}"},
                                                          {200, {}, "not JSON"},
                                                          {200, {}, raw_state("fan.other", "on").dump()},
                                                          {200, {}, Json{{"entity_id", "fan.room"}, {"state", "on"}, {"attributes", Json::array()}}.dump()}})
    {
        Fixture fixture;
        fixture.state_route(fan());
        fixture.service_route("fan/set_percentage");
        fixture.http.fake.routes.push_back({"GET", std::string(BASE_URL) + "/api/states/fan.room", response});
        const auto result = fixture.call("home_fan", fixture.bound({{"entity_id", "fan.room"}, {"percentage", 60}}));
        expect(result.at("request_status") == "accepted" && result.at("confirmed") == false && result.at("readback_errors").size() == 1 &&
                   result.at("observations").empty(),
               "readback failure must be returned as an unconfirmed receipt, not thrown after the effect");
        expect(fixture.http.fake.requests.size() == 3 && post_count(fixture) == 1, "readback failure must not retry or compensate");
    }
}

void test_secret_resolution_is_lazy_and_errors_are_redacted()
{
    Environment invalid(TOKEN_NAME, "fake\nInjected: secret");
    Fixture fixture;
    static_cast<void>(fixture.call("home_targets"));
    try
    {
        static_cast<void>(fixture.call("home_states", {{"entity_id", "light.a"}}));
        expect(false, "unsafe header token accepted");
    }
    catch (const Error &error)
    {
        expect(error.code() == "home_secret" && std::string(error.what()).find("Injected") == std::string::npos, "secret failure must be safe and specific");
    }
    expect(fixture.http.fake.requests.empty(), "invalid token must be rejected before transport access");
}

void test_runtime_default_approval_and_single_submission()
{
    HomeDirectory directory;
    auto http = std::make_unique<HomeHttp>();
    auto *fake = http.get();
    Runtime runtime(home_configuration(directory), std::move(http),
                    []()
                    {
                        return 1794433200LL;
                    });
    const auto targets = runtime.call("home_targets", Json::object());
    expect(targets.at("status") == "ok", "offline targets must be callable through the runtime");
    const Json arguments = {{"binding", targets.at("data").at("binding")}, {"entity_id", "fan.room"}, {"percentage", 60}};
    const auto pending = runtime.call("home_fan", arguments);
    expect(pending.at("status") == "pending" && pending.at("approval_id").is_string() && fake->fake.requests.empty(),
           "assistant effects must queue approval without even preflight reads");
    const auto rule_pending = runtime.call("home_fan", arguments, Origin::rule);
    expect(rule_pending.at("status") == "pending" && fake->fake.requests.empty(), "rule effects must also ask by default");
    const auto rule_id = rule_pending.at("approval_id").get<std::string>();
    expect(runtime.deny(rule_id).at("status") == "denied", "pending rule action must be deniable");
    expect(runtime.approve(rule_id).at("status") != "ok" && fake->fake.requests.empty(), "denied approval must not invoke a handler");
    fake->fake.add("GET", std::string(BASE_URL) + "/api/states/fan.room", fan());
    fake->fake.add("POST", std::string(BASE_URL) + "/api/services/fan/set_percentage", Json::array());
    fake->fake.add("GET", std::string(BASE_URL) + "/api/states/fan.room", fan(60));
    const auto id = pending.at("approval_id").get<std::string>();
    const auto executed = runtime.approve(id);
    expect(executed.at("status") == "ok", "approved action must reach the native handler");
    check_observed(executed.at("data"));
    expect(fake->fake.requests.size() == 3, "approved action must preflight against fresh capabilities");
    expect(runtime.approve(id).at("status") != "ok" && fake->fake.requests.size() == 3, "approval must be single-use, not an automatic resend token");
}

void test_runtime_reload_rejects_stale_approval()
{
    HomeDirectory directory;
    auto configuration = home_configuration(directory);
    std::string id;
    {
        auto http = std::make_unique<HomeHttp>();
        auto *fake = http.get();
        Runtime runtime(configuration, std::move(http),
                        []()
                        {
                            return 1794433200LL;
                        });
        const auto binding = runtime.call("home_targets", Json::object()).at("data").at("binding");
        const auto pending = runtime.call("home_fan", {{"binding", binding}, {"entity_id", "fan.room"}, {"percentage", 60}});
        expect(pending.at("status") == "pending" && fake->fake.requests.empty(), "queued action must survive without executing");
        id = pending.at("approval_id").get<std::string>();
    }
    configuration["connectors"]["home"]["fans"]["fan.room"]["maximum"] = 65;
    auto http = std::make_unique<HomeHttp>();
    auto *fake = http.get();
    Runtime reloaded(configuration, std::move(http),
                     []()
                     {
                         return 1794433201LL;
                     });
    const auto result = reloaded.approve(id);
    expect(result.at("status") == "invalid" && fake->fake.requests.empty(), "reload must revalidate the stored binding, even when the value fits new limits");
    expect(reloaded.approve(id).at("status") != "ok" && fake->fake.requests.empty(), "failed stale approval must not remain a reusable authorization");
}

void test_runtime_credential_rotation_preserves_review_binding()
{
    HomeDirectory directory;
    auto configuration = home_configuration(directory);
    std::string id;
    Json original_binding;
    {
        auto http = std::make_unique<HomeHttp>();
        Runtime runtime(configuration, std::move(http),
                        []()
                        {
                            return 1794433200LL;
                        });
        original_binding = runtime.call("home_targets", Json::object()).at("data").at("binding");
        const auto pending = runtime.call("home_fan", {{"binding", original_binding}, {"entity_id", "fan.room"}, {"percentage", 60}});
        expect(pending.at("status") == "pending", "rotation fixture must start with a pending action");
        id = pending.at("approval_id").get<std::string>();
    }
    Environment rotated("SWITCHBOARD_NATIVE_HOME_ROTATED_TEST_TOKEN", "native-home-rotated-token");
    configuration["connectors"]["home"]["token"] = "env:SWITCHBOARD_NATIVE_HOME_ROTATED_TEST_TOKEN";
    auto http = std::make_unique<HomeHttp>();
    auto *fake = http.get();
    fake->fake.add("GET", std::string(BASE_URL) + "/api/states/fan.room", fan());
    fake->fake.add("POST", std::string(BASE_URL) + "/api/services/fan/set_percentage", Json::array());
    fake->fake.add("GET", std::string(BASE_URL) + "/api/states/fan.room", fan(60));
    Runtime reloaded(configuration, std::move(http),
                     []()
                     {
                         return 1794433201LL;
                     });
    expect(reloaded.call("home_targets", Json::object()).at("data").at("binding") == original_binding,
           "credential reference rotation must not change reviewed target binding");
    const auto result = reloaded.approve(id);
    expect(result.at("status") == "ok" && fake->fake.requests.size() == 3, "unchanged reviewed approval must be usable once after credential rotation");
    check_observed(result.at("data"));
    expect(fake->fake.requests.front().headers.at("Authorization") == "Bearer native-home-rotated-token",
           "credentials must resolve only at request time under the current runtime");
}

void test_runtime_policy_rechecked_on_approval_and_human_calls()
{
    HomeDirectory directory;
    auto configuration = home_configuration(directory);
    std::string id;
    Json arguments;
    {
        auto http = std::make_unique<HomeHttp>();
        Runtime runtime(configuration, std::move(http),
                        []()
                        {
                            return 1794433200LL;
                        });
        arguments = {{"binding", runtime.call("home_targets", Json::object()).at("data").at("binding")}, {"entity_id", "fan.room"}, {"percentage", 60}};
        const auto pending = runtime.call("home_fan", arguments);
        expect(pending.at("status") == "pending", "policy fixture must begin pending");
        id = pending.at("approval_id").get<std::string>();
    }
    configuration["policy"]["tools"]["home_fan"] = {{"decision", "deny"}};
    auto http = std::make_unique<HomeHttp>();
    auto *fake = http.get();
    Runtime reloaded(configuration, std::move(http),
                     []()
                     {
                         return 1794433201LL;
                     });
    expect(reloaded.approve(id).at("status") == "denied" && fake->fake.requests.empty(), "approval must recheck current deny policy before provider access");
    expect(reloaded.call("home_fan", arguments, Origin::human).at("status") == "denied" && fake->fake.requests.empty(),
           "human origin must not bypass configured denial");
}
}  // namespace
}  // namespace switchboard::test

int main()
{
    using namespace switchboard::test;
    try
    {
        Environment token(TOKEN_NAME, "native-home-fake-token");
        test_disabled_and_offline_registration();
        test_settings_reject_unsupported_and_unsafe_scope();
        test_canonical_binding_and_reload();
        test_schemas_bind_targets_and_bound_shapes();
        test_plain_http_requires_explicit_opt_in();
        test_states_exact_requests_and_redaction();
        test_states_single_entity_room_and_validation();
        test_states_malformed_duplicate_and_capped_responses();
        test_capabilities_are_live_and_advisory();
        test_light_exact_request_and_observed_readback();
        test_multi_light_preflight_and_concrete_room_scope();
        test_light_rejects_conflicts_groups_and_missing_capabilities();
        test_climate_single_and_range_requests();
        test_climate_modes_and_fahrenheit();
        test_climate_refuses_unit_bounds_caps_and_change();
        test_fan_limits_change_and_zero();
        test_fan_rejects_unknown_percentage_and_features();
        test_media_actions_exact_requests();
        test_media_refuses_volume_group_features_and_values();
        test_readback_mismatch_unavailable_and_profile_tolerance();
        test_submission_unknown_and_rejected_without_retry();
        test_post_body_is_not_confirmation();
        test_readback_failure_is_reported_without_resend();
        test_secret_resolution_is_lazy_and_errors_are_redacted();
        test_runtime_default_approval_and_single_submission();
        test_runtime_reload_rejects_stale_approval();
        test_runtime_credential_rotation_preserves_review_binding();
        test_runtime_policy_rechecked_on_approval_and_human_calls();
        std::cout << "native home tests passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << error.what() << '\n';
        return 1;
    }
}

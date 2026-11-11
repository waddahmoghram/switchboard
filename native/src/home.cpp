#include "switchboard/native.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace switchboard
{
namespace
{
constexpr std::size_t HOME_READ_BYTES = 1024 * 1024;
constexpr std::size_t HOME_WRITE_BYTES = 65536;
constexpr std::int64_t HOME_TIMEOUT_SECONDS = 15;
constexpr std::int64_t FAN_SET_SPEED = 1;
constexpr std::int64_t CLIMATE_TEMPERATURE = 1;
constexpr std::int64_t CLIMATE_RANGE = 2;
constexpr std::int64_t MEDIA_PAUSE = 1;
constexpr std::int64_t MEDIA_VOLUME = 4;
constexpr std::int64_t MEDIA_MUTE = 8;
constexpr std::int64_t MEDIA_STOP = 4096;
constexpr std::int64_t MEDIA_PLAY = 16384;

[[noreturn]] void home_error(const std::string &message)
{
    throw Error("home", message);
}

bool slug(const std::string &value, const std::size_t maximum = 64)
{
    return !value.empty() && value.size() <= maximum &&
           std::all_of(value.begin(), value.end(),
                       [](const unsigned char character)
                       {
                           return (character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') || character == '_';
                       });
}

bool entity_id(const std::string &value)
{
    const auto dot = value.find('.');
    return dot != std::string::npos && slug(value.substr(0, dot)) && slug(value.substr(dot + 1), 190);
}

std::string domain_of(const std::string &value)
{
    return value.substr(0, value.find('.'));
}

bool safe_domain(const std::string &domain)
{
    static const std::set<std::string> domains{"sensor", "binary_sensor", "light", "climate", "fan", "media_player"};
    return domains.contains(domain);
}

bool includes(const Json &values, const std::string &value)
{
    return values.is_array() && std::find(values.begin(), values.end(), Json(value)) != values.end();
}

std::optional<double> number(const Json &value)
{
    if (!value.is_number())
    {
        return std::nullopt;
    }
    const double result = value.get<double>();
    return std::isfinite(result) ? std::optional<double>(result) : std::nullopt;
}

double bounded_number(const Json &value, const double minimum, const double maximum, const std::string &label)
{
    const auto result = number(value);
    if (!result || *result < minimum || *result > maximum)
    {
        home_error(label + " must be a finite number within its supported bounds");
    }
    return *result == 0 ? 0.0 : *result;
}

Json string_set(const Json &value, const std::size_t maximum, const bool nonempty, const std::function<bool(const std::string &)> &valid,
                const std::string &label)
{
    if (!value.is_array() || value.size() > maximum || (nonempty && value.empty()))
    {
        home_error(label + " must be a bounded list");
    }
    std::set<std::string> result;
    for (const auto &item : value)
    {
        if (!item.is_string() || !valid(item.get<std::string>()) || !result.insert(item.get<std::string>()).second)
        {
            home_error(label + " contains an invalid or duplicate entry");
        }
    }
    return Json(std::vector<std::string>(result.begin(), result.end()));
}

Json keys(const Json &object)
{
    Json result = Json::array();
    for (const auto &[key, value] : object.items())
    {
        static_cast<void>(value);
        result.push_back(key);
    }
    return result;
}

std::string endpoint(std::string value, const bool allow_http)
{
    if (std::any_of(value.begin(), value.end(),
                    [](const unsigned char character)
                    {
                        return character <= 32 || character >= 127;
                    }))
    {
        home_error("home base_url must be an ASCII server origin");
    }
    std::transform(value.begin(), value.end(), value.begin(),
                   [](const unsigned char character)
                   {
                       return static_cast<char>(std::tolower(character));
                   });
    const bool https = value.starts_with("https://");
    if (!https && !value.starts_with("http://"))
    {
        home_error("home base_url must use https, or explicitly permitted http");
    }
    const std::size_t offset = https ? 8 : 7;
    if (value.ends_with('/'))
    {
        value.pop_back();
    }
    const std::string authority = value.substr(offset);
    if (authority.empty() || authority.find_first_of("/\\@?#%") != std::string::npos)
    {
        home_error("home base_url must contain only an origin, without credentials or a path");
    }
    const auto port_at = authority.starts_with('[') ? authority.find(':', authority.find(']') == std::string::npos ? authority.size() : authority.find(']') + 1)
                                                    : authority.find(':');
    std::string host = port_at == std::string::npos ? authority : authority.substr(0, port_at);
    if (host.empty())
    {
        home_error("home base_url has no host");
    }
    if (!host.starts_with('[') && host.ends_with('.'))
    {
        host.pop_back();
    }
    std::string port;
    if (port_at != std::string::npos)
    {
        const auto text = authority.substr(port_at + 1);
        if (text.empty() || text.size() > 5 ||
            !std::all_of(text.begin(), text.end(),
                         [](const unsigned char character)
                         {
                             return character >= '0' && character <= '9';
                         }))
        {
            home_error("home base_url has an invalid port");
        }
        const auto numeric = std::stoi(text);
        if (numeric < 1 || numeric > 65535)
        {
            home_error("home base_url has an invalid port");
        }
        if (numeric != (https ? 443 : 80))
        {
            port = ":" + std::to_string(numeric);
        }
    }
    value = (https ? "https://" : "http://") + host + port;
    validate_endpoint(value, allow_http);
    return value;
}

struct HomeSettings
{
    std::string base_url;
    std::string token;
    bool allow_http = false;
    Json reviewed;
    std::string binding;

    [[nodiscard]] bool visible(const std::string &identifier) const
    {
        return entity_id(identifier) && safe_domain(domain_of(identifier)) && includes(reviewed.at("expose"), identifier) &&
               !includes(reviewed.at("deny_domains"), domain_of(identifier));
    }
};

HomeSettings load_home(const Json &input)
{
    check_object(input, {"base_url", "token", "allow_http", "expose", "deny_domains", "lights", "rooms", "climate", "fans", "media_players"},
                 {"base_url", "token"});
    HomeSettings settings;
    settings.allow_http = boolean_field(input, "allow_http");
    settings.base_url = endpoint(string_field(input, "base_url", 2048, true, 1), settings.allow_http);
    settings.token = string_field(input, "token", 2048, true, 5);
    if (settings.token.starts_with("env:"))
    {
        const auto name = settings.token.substr(4);
        const auto first = static_cast<unsigned char>(name.front());
        if (name.size() > 128 || !((first >= 'A' && first <= 'Z') || (first >= 'a' && first <= 'z') || first == '_') ||
            !std::all_of(name.begin(), name.end(),
                         [](const unsigned char character)
                         {
                             return (character >= 'A' && character <= 'Z') || (character >= 'a' && character <= 'z') ||
                                    (character >= '0' && character <= '9') || character == '_';
                         }))
        {
            home_error("home token needs a valid env: reference");
        }
    }
    else if (!settings.token.starts_with("file:") || settings.token.size() == 5 ||
             std::any_of(settings.token.begin(), settings.token.end(),
                         [](const unsigned char character)
                         {
                             return character < 32 || character == 127;
                         }))
    {
        home_error("home token must be an env: or file: secret reference");
    }
    settings.reviewed = {{"expose", string_set(
                                        input.value("expose", Json::array()), 200, false,
                                        [](const std::string &value)
                                        {
                                            return entity_id(value) && safe_domain(domain_of(value));
                                        },
                                        "home expose")},
                         {"deny_domains", string_set(
                                              input.value("deny_domains", Json::array()), 100, false,
                                              [](const std::string &value)
                                              {
                                                  return slug(value);
                                              },
                                              "home deny_domains")},
                         {"lights", Json::array()},
                         {"rooms", Json::object()},
                         {"climate", Json::object()},
                         {"fans", Json::object()},
                         {"media_players", Json::object()}};
    const auto approved_entities = [&settings](const Json &value, const std::string &domain, const bool nonempty, const std::string &label)
    {
        return string_set(
            value, 200, nonempty,
            [&settings, &domain](const std::string &identifier)
            {
                return settings.visible(identifier) && (domain.empty() || domain_of(identifier) == domain);
            },
            label);
    };
    settings.reviewed["lights"] = approved_entities(input.value("lights", Json::array()), "light", false, "home lights");
    const auto rooms = input.value("rooms", Json::object());
    if (!rooms.is_object() || rooms.size() > 100)
    {
        home_error("home rooms must be an object with at most 100 aliases");
    }
    for (const auto &[name, members] : rooms.items())
    {
        if (!slug(name))
        {
            home_error("home room aliases must be lowercase names of at most 64 characters");
        }
        settings.reviewed["rooms"][name] = approved_entities(members, "", true, "home room");
    }
    for (const auto &table_name : {"climate", "fans", "media_players"})
    {
        const auto table = input.value(table_name, Json::object());
        if (!table.is_object() || table.size() > 100)
        {
            home_error("home device profiles must be objects with at most 100 entries");
        }
        const std::string domain = std::string(table_name) == "fans" ? "fan" : (std::string(table_name) == "media_players" ? "media_player" : "climate");
        for (const auto &[identifier, values] : table.items())
        {
            if (!settings.visible(identifier) || domain_of(identifier) != domain)
            {
                home_error("home profiles must name exact exposed entities in the matching domain");
            }
            Json normalized;
            if (domain == "climate")
            {
                check_object(values, {"unit", "minimum", "maximum", "max_change", "modes"}, {"unit", "minimum", "maximum", "modes"});
                const auto unit = string_field(values, "unit", 1, true, 1);
                if (unit != "C" && unit != "F")
                {
                    home_error("home climate unit must be C or F; no conversion is supported");
                }
                const double lower = unit == "C" ? -50 : -58;
                const double upper = unit == "C" ? 100 : 212;
                const auto minimum = bounded_number(values.at("minimum"), lower, upper, "climate minimum");
                const auto maximum = bounded_number(values.at("maximum"), minimum, upper, "climate maximum");
                const auto change = bounded_number(values.value("max_change", Json(2)), 0.1, 20, "climate max_change");
                normalized = {{"unit", unit},
                              {"minimum", minimum},
                              {"maximum", maximum},
                              {"max_change", change},
                              {"modes", string_set(
                                            values.at("modes"), 5, true,
                                            [](const std::string &mode)
                                            {
                                                return mode == "off" || mode == "heat" || mode == "cool" || mode == "heat_cool" || mode == "fan_only";
                                            },
                                            "home climate modes")}};
            }
            else if (domain == "fan")
            {
                check_object(values, {"minimum", "maximum", "max_change"});
                const auto minimum = integer_field(values, "minimum", 0, 100, 0);
                normalized = {{"minimum", minimum},
                              {"maximum", integer_field(values, "maximum", minimum, 100, 100)},
                              {"max_change", integer_field(values, "max_change", 1, 100, 100)}};
            }
            else
            {
                check_object(values, {"minimum", "maximum"}, {"maximum"});
                const auto minimum = bounded_number(values.value("minimum", Json(0)), 0, 1, "media minimum");
                normalized = {{"minimum", minimum}, {"maximum", bounded_number(values.at("maximum"), minimum, 1, "media maximum")}};
            }
            settings.reviewed[table_name][identifier] = std::move(normalized);
        }
    }
    settings.binding = hash({{"component", "home"},
                             {"native_binding_version", 1},
                             {"base_url", settings.base_url},
                             {"allow_http", settings.allow_http},
                             {"reviewed", settings.reviewed}});
    return settings;
}

bool reported_leaf(const Json &raw)
{
    const auto &attributes = raw.at("attributes");
    for (const auto &key : {"entity_id", "group_members"})
    {
        if (!attributes.contains(key) || attributes.at(key).is_null())
        {
            continue;
        }
        const auto &members = attributes.at(key);
        if (!members.is_array() || members.size() > 1 || (members.size() == 1 && members.front() != raw.at("entity_id")))
        {
            return false;
        }
    }
    return true;
}

bool available(const Json &raw)
{
    return raw.at("state") != "unknown" && raw.at("state") != "unavailable";
}

bool feature(const Json &attributes, const std::int64_t wanted)
{
    const auto value = attributes.value("supported_features", Json());
    if (!value.is_number_integer() || value.is_boolean())
    {
        return false;
    }
    const auto numeric = number(value);
    if (!numeric || *numeric < 0 || *numeric > static_cast<double>(MAX_JSON_INTEGER))
    {
        return false;
    }
    return (value.get<std::int64_t>() & wanted) == wanted;
}

Json reported_strings(const Json &attributes, const std::string &name)
{
    const auto value = attributes.value(name, Json());
    if (!value.is_array() || value.size() > 16)
    {
        return Json::array();
    }
    std::set<std::string> seen;
    for (const auto &item : value)
    {
        if (!item.is_string() || item.get_ref<const std::string &>().size() > 64 || !seen.insert(item.get<std::string>()).second)
        {
            return Json::array();
        }
    }
    return value;
}

bool light_modes(const Json &modes)
{
    static const std::set<std::string> known{"onoff", "brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"};
    return !modes.empty() && std::all_of(modes.begin(), modes.end(),
                                         [](const Json &mode)
                                         {
                                             return known.contains(mode.get<std::string>());
                                         });
}

bool supports_color(const Json &modes)
{
    return includes(modes, "hs") || includes(modes, "xy") || includes(modes, "rgb") || includes(modes, "rgbw") || includes(modes, "rgbww");
}

bool supports_brightness(const Json &modes)
{
    return supports_color(modes) || includes(modes, "brightness") || includes(modes, "color_temp") || includes(modes, "white");
}

Json observation(const Json &raw, const std::string &observed_at)
{
    static const std::array names{"friendly_name", "device_class",     "unit_of_measurement", "state_class",           "current_temperature",
                                  "temperature",   "target_temp_low",  "target_temp_high",    "temperature_unit",      "min_temp",
                                  "max_temp",      "target_temp_step", "hvac_action",         "current_humidity",      "humidity",
                                  "brightness",    "color_mode",       "color_temp_kelvin",   "min_color_temp_kelvin", "max_color_temp_kelvin",
                                  "percentage",    "percentage_step",  "volume_level",        "is_volume_muted",       "supported_features"};
    Json attributes = Json::object();
    for (const auto &name : names)
    {
        const auto value = raw.at("attributes").value(name, Json());
        if ((value.is_string() && value.get_ref<const std::string &>().size() <= 200) || value.is_boolean() || number(value))
        {
            attributes[name] = value;
        }
    }
    for (const auto &name : {"supported_color_modes", "hvac_modes"})
    {
        const auto values = reported_strings(raw.at("attributes"), name);
        if (!values.empty())
        {
            attributes[name] = values;
        }
    }
    const auto color = raw.at("attributes").value("rgb_color", Json());
    if (color.is_array() && color.size() == 3 &&
        std::all_of(color.begin(), color.end(),
                    [](const Json &channel)
                    {
                        const auto value = number(channel);
                        return value && *value >= 0 && *value <= 255;
                    }))
    {
        attributes["rgb_color"] = color;
    }
    const auto safe_timestamp = [&raw](const std::string &key)
    {
        const auto value = raw.value(key, Json());
        return value.is_string() && value.get_ref<const std::string &>().size() <= 80 ? value : Json();
    };
    return {{"entity_id", raw.at("entity_id")},
            {"state", raw.at("state")},
            {"attributes", attributes},
            {"availability", available(raw) ? Json("available") : raw.at("state")},
            {"reported_leaf", reported_leaf(raw)},
            {"last_changed", safe_timestamp("last_changed")},
            {"last_updated", safe_timestamp("last_updated")},
            {"observed_at", observed_at}};
}

Json identifier_schema(const Json &allowed)
{
    // The schema subset needs a nonempty enum; "" cannot pass the entity pattern.
    return {{"type", "string"},
            {"minLength", 3},
            {"maxLength", 255},
            {"pattern", "^[a-z0-9_]{1,64}\\.[a-z0-9_]{1,190}$"},
            {"enum", allowed.empty() ? Json::array({""}) : allowed}};
}

struct HomePlan
{
    std::string domain;
    std::string service;
    std::vector<std::string> entities;
    Json data = Json::object();
    Json expected = Json::object();
    bool check_volume = false;
    std::int64_t required_feature = 0;
};

class Home
{
public:
    Home(Context &context, HomeSettings settings) : context_(context), settings_(std::move(settings))
    {
    }

    [[nodiscard]] Json action_schema(const std::string &name) const
    {
        const auto table_name = name == "home_lights" ? "lights" : (name == "home_climate" ? "climate" : (name == "home_fan" ? "fans" : "media_players"));
        const Json allowed = name == "home_lights" ? settings_.reviewed.at(table_name) : keys(settings_.reviewed.at(table_name));
        Json properties = {
            {"binding", {{"type", "string"}, {"minLength", 64}, {"maxLength", 64}, {"pattern", "^[0-9a-f]{64}$"}, {"enum", Json::array({settings_.binding})}}},
            {"room", room_schema()}};
        if (name == "home_lights")
        {
            properties["entity_ids"] = {{"type", "array"}, {"items", identifier_schema(allowed)}, {"minItems", 1}, {"maxItems", 16}};
            properties["state"] = {{"type", "string"}, {"enum", {"on", "off"}}};
            properties["brightness_pct"] = {{"type", "integer"}, {"minimum", 1}, {"maximum", 100}};
            properties["rgb_color"] = {{"type", "array"}, {"minItems", 3}, {"maxItems", 3}, {"items", {{"type", "integer"}, {"minimum", 0}, {"maximum", 255}}}};
            properties["color_temp_kelvin"] = {{"type", "integer"}, {"minimum", 1000}, {"maximum", 20000}};
            return object_schema(properties, {"binding", "entity_ids", "state"});
        }
        properties["entity_id"] = identifier_schema(allowed);
        if (name == "home_climate")
        {
            properties["unit"] = {{"type", "string"}, {"enum", {"C", "F"}}};
            for (const auto &key : {"temperature", "target_temp_low", "target_temp_high"})
            {
                properties[key] = {{"type", "number"}, {"minimum", -58}, {"maximum", 212}};
            }
            properties["hvac_mode"] = {{"type", "string"}, {"enum", {"off", "heat", "cool", "heat_cool", "fan_only"}}};
            return object_schema(properties, {"binding", "entity_id", "unit"});
        }
        if (name == "home_fan")
        {
            properties["percentage"] = {{"type", "integer"}, {"minimum", 0}, {"maximum", 100}};
            return object_schema(properties, {"binding", "entity_id", "percentage"});
        }
        properties["action"] = {{"type", "string"}, {"enum", {"play", "pause", "stop", "volume", "mute"}}};
        properties["volume_level"] = {{"type", "number"}, {"minimum", 0}, {"maximum", 1}};
        properties["is_volume_muted"] = {{"type", "boolean"}};
        return object_schema(properties, {"binding", "entity_id", "action"});
    }

    [[nodiscard]] Json states_schema() const
    {
        return object_schema({{"entity_id", identifier_schema(visible_entities())},
                              {"domain", {{"type", "string"}, {"enum", {"sensor", "binary_sensor", "light", "climate", "fan", "media_player"}}}},
                              {"room", room_schema()},
                              {"limit", {{"type", "integer"}, {"minimum", 1}, {"maximum", 200}}}});
    }

    [[nodiscard]] Json capabilities_schema() const
    {
        return object_schema({{"entity_id", identifier_schema(visible_entities())}, {"room", room_schema()}}, {"entity_id"});
    }

    [[nodiscard]] Json targets(const Json &arguments) const
    {
        validate(arguments, object_schema(Json::object()));
        auto result = settings_.reviewed;
        result["binding"] = settings_.binding;
        result["room_targeting"] = "explicit entities only; room restricts targets and never expands them";
        result["capabilities_source"] = "home_capabilities reads reported state attributes; execution rechecks them";
        result["physical_delivery_verified"] = false;
        return result;
    }

    [[nodiscard]] Json states(const Json &arguments)
    {
        validate(arguments, states_schema());
        std::vector<Json> selected;
        if (arguments.contains("entity_id"))
        {
            const auto identifier = arguments.at("entity_id").get<std::string>();
            require_scope(identifier, arguments);
            if (arguments.contains("domain") && domain_of(identifier) != arguments.at("domain"))
            {
                home_error("home entity does not belong to the requested domain");
            }
            selected.push_back(read_state(identifier));
        }
        else
        {
            const auto response = get("/api/states");
            if (!response.is_array() || response.size() > 10000)
            {
                home_error("Home Assistant returned an invalid or excessive states list");
            }
            std::set<std::string> seen;
            for (const auto &raw : response)
            {
                check_state(raw);
                const auto identifier = raw.at("entity_id").get<std::string>();
                if (!seen.insert(identifier).second)
                {
                    home_error("Home Assistant returned duplicate states");
                }
                if (!settings_.visible(identifier) ||
                    (arguments.contains("room") && !includes(settings_.reviewed.at("rooms").at(arguments.at("room").get<std::string>()), identifier)) ||
                    (arguments.contains("domain") && domain_of(identifier) != arguments.at("domain")))
                {
                    continue;
                }
                selected.push_back(raw);
            }
        }
        std::sort(selected.begin(), selected.end(),
                  [](const Json &left, const Json &right)
                  {
                      return left.at("entity_id").get<std::string>() < right.at("entity_id").get<std::string>();
                  });
        const auto limit = static_cast<std::size_t>(integer_field(arguments, "limit", 1, 200, 100));
        const bool truncated = selected.size() > limit;
        Json entities = Json::array();
        const auto observed_at = context_.timestamp();
        for (std::size_t index = 0; index < std::min(limit, selected.size()); ++index)
        {
            entities.push_back(observation(selected[index], observed_at));
        }
        return {{"entities", entities}, {"truncated", truncated}, {"freshness_basis", "time of local read, not proof of device freshness"}};
    }

    [[nodiscard]] Json capabilities(const Json &arguments)
    {
        validate(arguments, capabilities_schema());
        const auto identifier = arguments.at("entity_id").get<std::string>();
        require_scope(identifier, arguments);
        const auto raw = read_state(identifier);
        const auto &attributes = raw.at("attributes");
        const auto domain = domain_of(identifier);
        Json supported = Json::object();
        Json profile;
        if (domain == "light")
        {
            const auto modes = reported_strings(attributes, "supported_color_modes");
            supported = {{"on_off", light_modes(modes)},
                         {"brightness_pct", light_modes(modes) && supports_brightness(modes)},
                         {"rgb_color", light_modes(modes) && supports_color(modes)},
                         {"color_temp_kelvin", light_modes(modes) && includes(modes, "color_temp")}};
            if (includes(settings_.reviewed.at("lights"), identifier))
            {
                profile = Json::object();
            }
        }
        else if (domain == "climate")
        {
            supported = {{"temperature", feature(attributes, CLIMATE_TEMPERATURE)},
                         {"target_range", feature(attributes, CLIMATE_RANGE)},
                         {"hvac_modes", reported_strings(attributes, "hvac_modes")}};
            profile = settings_.reviewed.at("climate").value(identifier, Json());
        }
        else if (domain == "fan")
        {
            supported["percentage"] = feature(attributes, FAN_SET_SPEED);
            profile = settings_.reviewed.at("fans").value(identifier, Json());
        }
        else if (domain == "media_player")
        {
            supported = {{"play", feature(attributes, MEDIA_PLAY)},
                         {"pause", feature(attributes, MEDIA_PAUSE)},
                         {"stop", feature(attributes, MEDIA_STOP)},
                         {"volume", feature(attributes, MEDIA_VOLUME)},
                         {"mute", feature(attributes, MEDIA_MUTE)}};
            profile = settings_.reviewed.at("media_players").value(identifier, Json());
        }
        return {{"binding", settings_.binding},
                {"entity_id", identifier},
                {"domain", domain},
                {"profiled", !profile.is_null()},
                {"profile", profile},
                {"reported_support", supported},
                {"observation", observation(raw, context_.timestamp())},
                {"limitation", "reported capabilities are advisory; action preflight is required"}};
    }

    [[nodiscard]] Json action(const std::string &name, const Json &arguments)
    {
        if (arguments.is_object() && arguments.contains("binding") && arguments.at("binding").is_string() && arguments.at("binding") != settings_.binding)
        {
            throw Error("home_binding", "home configuration changed; read home_targets and request new approval");
        }
        validate(arguments, action_schema(name));
        HomePlan plan;
        if (name == "home_lights")
        {
            plan = light_plan(arguments);
        }
        else if (name == "home_climate")
        {
            plan = climate_plan(arguments);
        }
        else if (name == "home_fan")
        {
            plan = fan_plan(arguments);
        }
        else
        {
            plan = media_plan(arguments);
        }
        return execute(plan);
    }

private:
    [[nodiscard]] Json visible_entities() const
    {
        Json result = Json::array();
        for (const auto &value : settings_.reviewed.at("expose"))
        {
            if (settings_.visible(value.get<std::string>()))
            {
                result.push_back(value);
            }
        }
        return result;
    }

    [[nodiscard]] Json room_schema() const
    {
        const auto names = keys(settings_.reviewed.at("rooms"));
        return {{"type", "string"}, {"minLength", 1}, {"maxLength", 64}, {"pattern", "^[a-z0-9_]{1,64}$"}, {"enum", names.empty() ? Json::array({""}) : names}};
    }

    void require_scope(const std::string &identifier, const Json &arguments) const
    {
        if (!settings_.visible(identifier))
        {
            home_error("home target is not exactly exposed or is denied");
        }
        if (arguments.contains("room"))
        {
            const auto name = arguments.at("room").get<std::string>();
            const auto &rooms = settings_.reviewed.at("rooms");
            if (!rooms.contains(name) || !includes(rooms.at(name), identifier))
            {
                home_error("home target is not a member of that explicit room");
            }
        }
    }

    [[nodiscard]] HttpRequest request(const std::string &method, const std::string &path) const
    {
        std::string token;
        try
        {
            token = context_.secret(settings_.token);
        }
        catch (const std::exception &)
        {
            throw Error("home_secret", "home token reference could not be resolved");
        }
        if (token.empty() || token.size() > 16384 ||
            !std::all_of(token.begin(), token.end(),
                         [](const unsigned char character)
                         {
                             return character >= 33 && character <= 126;
                         }))
        {
            throw Error("home_secret", "home token contains invalid header characters");
        }
        HttpRequest result;
        result.method = method;
        result.url = settings_.base_url + path;
        result.headers = {{"Authorization", "Bearer " + token}, {"Accept", "application/json"}};
        result.max_bytes = method == "GET" ? HOME_READ_BYTES : HOME_WRITE_BYTES;
        result.timeout_seconds = HOME_TIMEOUT_SECONDS;
        result.allow_http = settings_.allow_http;
        if (method == "POST")
        {
            result.headers["Content-Type"] = "application/json";
        }
        return result;
    }

    [[nodiscard]] Json get(const std::string &path) const
    {
        const auto input = request("GET", path);
        HttpResponse response;
        try
        {
            response = context_.http.request(input);
        }
        catch (const std::exception &)
        {
            throw Error("home_http", "Home Assistant state read failed");
        }
        if (response.status < 200 || response.status >= 300 || response.body.size() > input.max_bytes)
        {
            throw Error("home_http", "Home Assistant state read was refused or exceeded its response cap");
        }
        try
        {
            return response_json(response);
        }
        catch (const std::exception &)
        {
            throw Error("home_response", "Home Assistant state response is not valid bounded JSON");
        }
    }

    static void check_state(const Json &raw)
    {
        if (!raw.is_object() || !raw.contains("entity_id") || !raw.at("entity_id").is_string() || !entity_id(raw.at("entity_id").get<std::string>()) ||
            !raw.contains("state") || !raw.at("state").is_string() || raw.at("state").get_ref<const std::string &>().size() > 255 ||
            !raw.contains("attributes") || !raw.at("attributes").is_object())
        {
            home_error("Home Assistant returned a malformed entity state");
        }
    }

    [[nodiscard]] Json read_state(const std::string &identifier) const
    {
        const auto raw = get("/api/states/" + identifier);
        check_state(raw);
        if (raw.at("entity_id") != identifier)
        {
            home_error("Home Assistant returned a different entity");
        }
        return raw;
    }

    [[nodiscard]] Json read_target(const std::string &identifier) const
    {
        const auto raw = read_state(identifier);
        if (!available(raw) || !reported_leaf(raw))
        {
            home_error("home target is unavailable or reports a group; no action was sent");
        }
        return raw;
    }

    static void require_feature(const Json &attributes, const std::int64_t wanted)
    {
        if (!feature(attributes, wanted))
        {
            home_error("home target does not report the required capability; no action was sent");
        }
    }

    [[nodiscard]] HomePlan light_plan(const Json &arguments) const
    {
        HomePlan plan;
        plan.domain = "light";
        plan.service = arguments.at("state") == "on" ? "turn_on" : "turn_off";
        plan.entities = arguments.at("entity_ids").get<std::vector<std::string>>();
        std::set<std::string> seen;
        for (const auto &identifier : plan.entities)
        {
            require_scope(identifier, arguments);
            if (!includes(settings_.reviewed.at("lights"), identifier) || !seen.insert(identifier).second)
            {
                home_error("home light targets must be distinct reviewed lights");
            }
        }
        if ((arguments.at("state") == "off" &&
             (arguments.contains("brightness_pct") || arguments.contains("rgb_color") || arguments.contains("color_temp_kelvin"))) ||
            (arguments.contains("rgb_color") && arguments.contains("color_temp_kelvin")))
        {
            home_error("home light request combines incompatible settings");
        }
        for (const auto &key : {"brightness_pct", "rgb_color", "color_temp_kelvin"})
        {
            if (arguments.contains(key))
            {
                plan.data[key] = arguments.at(key);
            }
        }
        for (const auto &identifier : plan.entities)
        {
            const auto raw = read_target(identifier);
            const auto &attributes = raw.at("attributes");
            const auto modes = reported_strings(attributes, "supported_color_modes");
            if (!light_modes(modes))
            {
                home_error("home light does not report known color modes");
            }
            Json wanted = {{"state", arguments.at("state")}};
            if (arguments.contains("brightness_pct"))
            {
                if (!supports_brightness(modes))
                {
                    home_error("home light does not report brightness support");
                }
                wanted["brightness"] = std::lround(arguments.at("brightness_pct").get<double>() * 255 / 100);
            }
            if (arguments.contains("rgb_color"))
            {
                if (!supports_color(modes))
                {
                    home_error("home light does not report color support");
                }
                wanted["rgb_color"] = arguments.at("rgb_color");
            }
            if (arguments.contains("color_temp_kelvin"))
            {
                const auto lower = number(attributes.value("min_color_temp_kelvin", Json()));
                const auto upper = number(attributes.value("max_color_temp_kelvin", Json()));
                const auto kelvin = arguments.at("color_temp_kelvin").get<double>();
                if (!includes(modes, "color_temp") || !lower || !upper || *lower > *upper || kelvin < *lower || kelvin > *upper)
                {
                    home_error("home light color temperature exceeds its reported capability");
                }
                wanted["color_temp_kelvin"] = arguments.at("color_temp_kelvin");
                wanted["color_mode"] = "color_temp";
            }
            plan.expected[identifier] = std::move(wanted);
        }
        return plan;
    }

    [[nodiscard]] HomePlan climate_plan(const Json &arguments) const
    {
        const auto identifier = arguments.at("entity_id").get<std::string>();
        require_scope(identifier, arguments);
        const auto &profiles = settings_.reviewed.at("climate");
        if (!profiles.contains(identifier))
        {
            home_error("home climate target has no reviewed profile");
        }
        const auto &limits = profiles.at(identifier);
        if (arguments.at("unit") != limits.at("unit"))
        {
            home_error("home climate unit differs from its profile; no conversion is performed");
        }
        const bool single = arguments.contains("temperature");
        const bool low = arguments.contains("target_temp_low");
        const bool high = arguments.contains("target_temp_high");
        const bool temperatures = single || low || high;
        if ((single && (low || high)) || low != high || (!temperatures && !arguments.contains("hvac_mode")) ||
            (low && arguments.at("target_temp_low").get<double>() > arguments.at("target_temp_high").get<double>()))
        {
            home_error("home climate needs one setpoint, an ordered complete range, or a mode");
        }
        if (arguments.contains("hvac_mode") && !includes(limits.at("modes"), arguments.at("hvac_mode").get<std::string>()))
        {
            home_error("home climate mode is not reviewed");
        }
        const auto raw = read_target(identifier);
        const auto &attributes = raw.at("attributes");
        const auto modes = reported_strings(attributes, "hvac_modes");
        const auto mode = arguments.contains("hvac_mode") ? arguments.at("hvac_mode").get<std::string>() : raw.at("state").get<std::string>();
        if (!includes(modes, mode) || !includes(limits.at("modes"), mode))
        {
            home_error("home climate mode is not both reported and reviewed");
        }
        HomePlan plan;
        plan.domain = "climate";
        plan.service = temperatures ? "set_temperature" : "set_hvac_mode";
        plan.entities = {identifier};
        Json wanted = {{"state", mode}};
        if (temperatures || mode == "heat" || mode == "cool" || mode == "heat_cool")
        {
            const bool ranged = mode == "heat_cool";
            if ((mode != "heat" && mode != "cool" && !ranged) || (temperatures && (low != ranged)))
            {
                home_error("home climate heat/cool needs one setpoint; heat_cool needs a complete range");
            }
            require_feature(attributes, ranged ? CLIMATE_RANGE : CLIMATE_TEMPERATURE);
            const auto unit = std::string("\xc2\xb0") + limits.at("unit").get<std::string>();
            if (attributes.value("temperature_unit", Json()) != unit)
            {
                home_error("home climate reported temperature unit is missing or different");
            }
            const auto lower = number(attributes.value("min_temp", Json()));
            const auto upper = number(attributes.value("max_temp", Json()));
            if (!lower || !upper || *lower > *upper)
            {
                home_error("home climate does not report ordered temperature bounds");
            }
            const std::vector<std::string> fields =
                ranged ? std::vector<std::string>{"target_temp_low", "target_temp_high"} : std::vector<std::string>{"temperature"};
            for (const auto &key : fields)
            {
                const auto previous = number(attributes.value(key, Json()));
                const auto candidate = temperatures ? number(arguments.at(key)) : previous;
                if (!candidate || *candidate < std::max(*lower, limits.at("minimum").get<double>()) ||
                    *candidate > std::min(*upper, limits.at("maximum").get<double>()) ||
                    (temperatures && (!previous || std::abs(*candidate - *previous) > limits.at("max_change").get<double>())))
                {
                    home_error("home climate setpoint is unknown, out of bounds, or exceeds max_change");
                }
                wanted[key] = *candidate;
                if (temperatures)
                {
                    plan.data[key] = arguments.at(key);
                }
            }
            if (ranged && wanted.at("target_temp_low").get<double>() > wanted.at("target_temp_high").get<double>())
            {
                home_error("home climate existing range is not ordered");
            }
            wanted["temperature_unit"] = unit;
        }
        if (arguments.contains("hvac_mode"))
        {
            plan.data["hvac_mode"] = mode;
        }
        plan.expected[identifier] = std::move(wanted);
        return plan;
    }

    [[nodiscard]] HomePlan fan_plan(const Json &arguments) const
    {
        const auto identifier = arguments.at("entity_id").get<std::string>();
        require_scope(identifier, arguments);
        const auto &profiles = settings_.reviewed.at("fans");
        if (!profiles.contains(identifier))
        {
            home_error("home fan target has no reviewed profile");
        }
        const auto &limits = profiles.at(identifier);
        const auto percentage = arguments.at("percentage").get<std::int64_t>();
        if (percentage < limits.at("minimum").get<std::int64_t>() || percentage > limits.at("maximum").get<std::int64_t>())
        {
            home_error("home fan percentage exceeds its profile");
        }
        const auto raw = read_target(identifier);
        const auto &attributes = raw.at("attributes");
        require_feature(attributes, FAN_SET_SPEED);
        const auto previous = number(attributes.value("percentage", Json()));
        if (!previous || *previous < 0 || *previous > 100 || std::abs(percentage - *previous) > limits.at("max_change").get<double>())
        {
            home_error("home fan prior percentage is unknown or change exceeds max_change");
        }
        HomePlan plan;
        plan.domain = "fan";
        plan.service = "set_percentage";
        plan.entities = {identifier};
        plan.data = {{"percentage", percentage}};
        plan.expected[identifier] = {{"percentage", percentage}, {"state", percentage == 0 ? "off" : "on"}};
        return plan;
    }

    [[nodiscard]] HomePlan media_plan(const Json &arguments) const
    {
        const auto identifier = arguments.at("entity_id").get<std::string>();
        require_scope(identifier, arguments);
        const auto &profiles = settings_.reviewed.at("media_players");
        if (!profiles.contains(identifier))
        {
            home_error("home media target has no reviewed profile");
        }
        const auto action = arguments.at("action").get<std::string>();
        if (arguments.contains("volume_level") != (action == "volume") || arguments.contains("is_volume_muted") != (action == "mute"))
        {
            home_error("home media accepts only the value required by the selected action");
        }
        const auto &limits = profiles.at(identifier);
        if (action == "volume")
        {
            static_cast<void>(
                bounded_number(arguments.at("volume_level"), limits.at("minimum").get<double>(), limits.at("maximum").get<double>(), "home media volume"));
        }
        const auto raw = read_target(identifier);
        const auto &attributes = raw.at("attributes");
        static const std::map<std::string, std::pair<std::string, std::int64_t>> actions{{"play", {"media_play", MEDIA_PLAY}},
                                                                                         {"pause", {"media_pause", MEDIA_PAUSE}},
                                                                                         {"stop", {"media_stop", MEDIA_STOP}},
                                                                                         {"volume", {"volume_set", MEDIA_VOLUME}},
                                                                                         {"mute", {"volume_mute", MEDIA_MUTE}}};
        const auto &[service, required] = actions.at(action);
        require_feature(attributes, required);
        HomePlan plan;
        plan.domain = "media_player";
        plan.service = service;
        plan.entities = {identifier};
        plan.required_feature = required;
        plan.check_volume = action == "play" || action == "volume" || (action == "mute" && !arguments.at("is_volume_muted").get<bool>());
        if (action == "play" || (action == "mute" && !arguments.at("is_volume_muted").get<bool>()))
        {
            static_cast<void>(bounded_number(attributes.value("volume_level", Json()), limits.at("minimum").get<double>(), limits.at("maximum").get<double>(),
                                             "home observed volume"));
        }
        Json wanted = Json::object();
        if (action == "volume" || action == "mute")
        {
            const auto key = action == "volume" ? "volume_level" : "is_volume_muted";
            plan.data[key] = arguments.at(key);
            wanted[key] = arguments.at(key);
        }
        else
        {
            wanted["state"] = action == "stop" ? Json::array({"idle", "off"}) : Json(action == "play" ? "playing" : "paused");
        }
        plan.expected[identifier] = std::move(wanted);
        return plan;
    }

    [[nodiscard]] bool within_profile(const HomePlan &plan, const std::string &identifier, const Json &raw) const
    {
        if (!available(raw) || !reported_leaf(raw))
        {
            return false;
        }
        const auto &attributes = raw.at("attributes");
        const auto &wanted = plan.expected.at(identifier);
        if (plan.required_feature != 0 && !feature(attributes, plan.required_feature))
        {
            return false;
        }
        if (plan.domain == "light")
        {
            const auto modes = reported_strings(attributes, "supported_color_modes");
            if (!light_modes(modes))
            {
                return false;
            }
            if (wanted.contains("brightness"))
            {
                const auto brightness = number(attributes.value("brightness", Json()));
                if (!supports_brightness(modes) || !brightness || *brightness < 0 || *brightness > 255)
                {
                    return false;
                }
            }
            if (wanted.contains("rgb_color"))
            {
                const auto color = attributes.value("rgb_color", Json());
                if (!supports_color(modes) || !color.is_array() || color.size() != 3 ||
                    !std::all_of(color.begin(), color.end(),
                                 [](const Json &channel)
                                 {
                                     const auto value = number(channel);
                                     return value && *value >= 0 && *value <= 255;
                                 }))
                {
                    return false;
                }
            }
            if (wanted.contains("color_temp_kelvin"))
            {
                const auto value = number(attributes.value("color_temp_kelvin", Json()));
                const auto lower = number(attributes.value("min_color_temp_kelvin", Json()));
                const auto upper = number(attributes.value("max_color_temp_kelvin", Json()));
                if (!includes(modes, "color_temp") || !value || !lower || !upper || *lower > *upper || *value < *lower || *value > *upper)
                {
                    return false;
                }
            }
        }
        else if (plan.domain == "climate")
        {
            const auto &limits = settings_.reviewed.at("climate").at(identifier);
            const auto mode = raw.at("state").get<std::string>();
            if (!includes(limits.at("modes"), mode) || !includes(reported_strings(attributes, "hvac_modes"), mode))
            {
                return false;
            }
            if (wanted.contains("temperature_unit"))
            {
                const auto lower = number(attributes.value("min_temp", Json()));
                const auto upper = number(attributes.value("max_temp", Json()));
                if (!lower || !upper || *lower > *upper || attributes.value("temperature_unit", Json()) != wanted.at("temperature_unit") ||
                    !feature(attributes, wanted.contains("temperature") ? CLIMATE_TEMPERATURE : CLIMATE_RANGE))
                {
                    return false;
                }
                for (const auto &key : {"temperature", "target_temp_low", "target_temp_high"})
                {
                    if (wanted.contains(key))
                    {
                        const auto value = number(attributes.value(key, Json()));
                        if (!value || *value < std::max(*lower, limits.at("minimum").get<double>()) ||
                            *value > std::min(*upper, limits.at("maximum").get<double>()))
                        {
                            return false;
                        }
                    }
                }
                if (wanted.contains("target_temp_low") && attributes.at("target_temp_low").get<double>() > attributes.at("target_temp_high").get<double>())
                {
                    return false;
                }
            }
        }
        else if (plan.domain == "fan")
        {
            const auto &limits = settings_.reviewed.at("fans").at(identifier);
            const auto value = number(attributes.value("percentage", Json()));
            if (!feature(attributes, FAN_SET_SPEED) || !value || *value < limits.at("minimum").get<double>() || *value > limits.at("maximum").get<double>())
            {
                return false;
            }
        }
        else if (plan.check_volume)
        {
            const auto &limits = settings_.reviewed.at("media_players").at(identifier);
            const auto value = number(attributes.value("volume_level", Json()));
            if (!value || *value < limits.at("minimum").get<double>() || *value > limits.at("maximum").get<double>())
            {
                return false;
            }
        }
        return true;
    }

    static bool matches(const Json &raw, const Json &expected)
    {
        if (!available(raw))
        {
            return false;
        }
        for (const auto &[key, wanted] : expected.items())
        {
            const auto actual = key == "state" ? raw.at("state") : raw.at("attributes").value(key, Json());
            if (key == "state" && wanted.is_array())
            {
                if (std::find(wanted.begin(), wanted.end(), actual) == wanted.end())
                {
                    return false;
                }
            }
            else if (key == "rgb_color")
            {
                if (!actual.is_array() || actual.size() != 3)
                {
                    return false;
                }
                for (std::size_t index = 0; index < 3; ++index)
                {
                    const auto channel = number(actual[index]);
                    if (!channel || *channel < 0 || *channel > 255 || std::abs(*channel - wanted[index].get<double>()) > 1)
                    {
                        return false;
                    }
                }
            }
            else if (wanted.is_number())
            {
                const auto value = number(actual);
                const auto tolerance = key == "brightness" ? 1.0 : (key == "volume_level" ? 0.01 : 0.05);
                if (!value || std::abs(*value - wanted.get<double>()) > tolerance)
                {
                    return false;
                }
            }
            else if (actual != wanted)
            {
                return false;
            }
        }
        return true;
    }

    [[nodiscard]] Json execute(const HomePlan &plan) const
    {
        auto input = request("POST", "/api/services/" + plan.domain + "/" + plan.service);
        Json body = plan.data;
        body["entity_id"] = plan.entities.size() == 1 ? Json(plan.entities.front()) : Json(plan.entities);
        input.body = body.dump();
        std::string request_status = "unknown";
        Json error = "submission outcome unknown; inspect state before a new request; nothing was resent";
        try
        {
            const auto response = context_.http.request(input);
            if (response.body.size() <= input.max_bytes && response.status >= 200 && response.status < 300)
            {
                request_status = "accepted";
                error = nullptr;
            }
            else if (response.body.size() <= input.max_bytes && response.status >= 300 && response.status < 500 && response.status != 408)
            {
                request_status = "rejected";
                error = "Home Assistant refused the action; nothing was resent";
            }
        }
        catch (const std::exception &)
        {
        }
        Json observations = Json::array();
        Json readback_errors = Json::array();
        bool confirmed = request_status == "accepted";
        if (request_status != "rejected")
        {
            for (const auto &identifier : plan.entities)
            {
                try
                {
                    const auto raw = read_state(identifier);
                    auto snapshot = observation(raw, context_.timestamp());
                    const bool match = matches(raw, plan.expected.at(identifier));
                    const bool within = within_profile(plan, identifier, raw);
                    snapshot["matches_request"] = match;
                    snapshot["within_profile"] = within;
                    observations.push_back(std::move(snapshot));
                    confirmed = confirmed && match && within;
                }
                catch (const std::exception &)
                {
                    confirmed = false;
                    readback_errors.push_back({{"entity_id", identifier}, {"error", "readback failed; the effect was not resent"}});
                }
            }
        }
        return {{"called", plan.domain + "." + plan.service},
                {"entity_ids", plan.entities},
                {"request_status", request_status},
                {"status", request_status == "rejected" ? "rejected" : (confirmed ? "observed" : "requested_unconfirmed")},
                {"confirmed", confirmed},
                {"physical_delivery_verified", false},
                {"confirmation_scope", "reported requested state only; not physical completion or causal proof"},
                {"observations", observations},
                {"readback_errors", readback_errors},
                {"error", error},
                {"retry_safe", false}};
    }

    Context &context_;
    const HomeSettings settings_;
};
}  // namespace

void register_home(Context &context, Registry &registry)
{
    if (!context.enabled("home"))
    {
        return;
    }
    auto home = std::make_shared<Home>(context, load_home(context.settings("home")));
    registry.add({"home_targets",
                  "Read exact reviewed home targets, room scopes, limits and current binding; offline.",
                  Tier::read,
                  object_schema(Json::object()),
                  [home](const Json &arguments)
                  {
                      return home->targets(arguments);
                  },
                  {}});
    registry.add({"home_states",
                  "Read only exact exposed home states; returned attributes are untrusted observations.",
                  Tier::read,
                  home->states_schema(),
                  [home](const Json &arguments)
                  {
                      return home->states(arguments);
                  },
                  {}});
    registry.add({"home_capabilities",
                  "Read one exposed entity's reported capabilities and reviewed profile.",
                  Tier::read,
                  home->capabilities_schema(),
                  [home](const Json &arguments)
                  {
                      return home->capabilities(arguments);
                  },
                  {}});
    const std::map<std::string, std::string> actions{{"home_lights", "Set explicit reviewed leaf lights on/off, brightness or color; no fades or groups."},
                                                     {"home_climate", "Set one reviewed thermostat's unit-bound setpoint, range or mode within limits."},
                                                     {"home_fan", "Set one reviewed fan's bounded percentage; no inferred speed conversion."},
                                                     {"home_media", "Play/pause/stop or set bounded volume/mute on one reviewed ungrouped player."}};
    for (const auto &[name, description] : actions)
    {
        registry.add({name, description, Tier::act, home->action_schema(name),
                      [home, name](const Json &arguments)
                      {
                          return home->action(name, arguments);
                      },
                      [name](const Json &arguments)
                      {
                          return name + " " + arguments.dump();
                      }});
    }
}
}  // namespace switchboard

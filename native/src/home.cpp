#include "switchboard/native.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

#if !defined(__cpp_lib_to_chars)
#include <locale>
#include <sstream>
#endif

namespace switchboard
{
namespace
{
constexpr std::size_t HOME_READ_BYTES = 1024 * 1024;
constexpr std::size_t HOME_WRITE_BYTES = 65536;
constexpr std::int64_t HOME_TIMEOUT_SECONDS = 15;
constexpr std::int64_t POLL_STALE_MAXIMUM = 259200;
constexpr std::size_t SNAPSHOT_BYTES = 8192;
constexpr std::size_t PRESET_TARGETS = 16;
constexpr std::array<const char *, 7> SENSOR_KINDS{"temperature", "humidity", "motion", "leak", "battery", "power", "energy"};
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

struct PollSettings
{
    bool enabled = false;
    std::vector<std::string> entities;
    std::int64_t interval_seconds = 60;
    std::int64_t stale_after_seconds = 300;
};

struct HomeSettings
{
    std::string base_url;
    std::string token;
    bool allow_http = false;
    Json reviewed;
    std::string binding;
    std::string source;
    PollSettings poll;

    [[nodiscard]] bool visible(const std::string &identifier) const
    {
        return entity_id(identifier) && safe_domain(domain_of(identifier)) && includes(reviewed.at("expose"), identifier) &&
               !includes(reviewed.at("deny_domains"), domain_of(identifier));
    }
};

PollSettings load_poll(const Json &input, const HomeSettings &settings)
{
    check_object(input, {"enabled", "entities", "interval_seconds", "stale_after_seconds"});
    PollSettings poll;
    poll.enabled = boolean_field(input, "enabled");
    poll.interval_seconds = integer_field(input, "interval_seconds", 30, 86400, 60);
    poll.stale_after_seconds =
        integer_field(input, "stale_after_seconds", poll.interval_seconds, POLL_STALE_MAXIMUM, std::max<std::int64_t>(300, 3 * poll.interval_seconds));
    poll.entities = string_set(
                        input.value("entities", Json::array()), 200, poll.enabled,
                        [&settings](const std::string &identifier)
                        {
                            return settings.visible(identifier);
                        },
                        "home poll entities")
                        .get<std::vector<std::string>>();
    return poll;
}

Json preset_target(const Json &value)
{
    check_object(value, {"state", "brightness_pct", "rgb_color", "color_temp_kelvin"}, {"state"});
    const auto state = string_field(value, "state", 3, true, 2);
    if (state != "on" && state != "off")
    {
        home_error("home lighting preset state must be on or off");
    }
    Json result = {{"state", state}};
    if (value.contains("brightness_pct"))
    {
        result["brightness_pct"] = integer_field(value, "brightness_pct", 1, 100, 1);
    }
    if (value.contains("color_temp_kelvin"))
    {
        result["color_temp_kelvin"] = integer_field(value, "color_temp_kelvin", 1000, 20000, 1000);
    }
    if (value.contains("rgb_color"))
    {
        const auto &color = value.at("rgb_color");
        if (!color.is_array() || color.size() != 3 ||
            !std::all_of(color.begin(), color.end(),
                         [](const Json &channel)
                         {
                             return channel.is_number_integer() && channel.get<double>() >= 0 && channel.get<double>() <= 255;
                         }))
        {
            home_error("home lighting preset rgb_color needs three integer channels from 0 to 255");
        }
        Json channels = Json::array();
        for (const auto &channel : color)
        {
            channels.push_back(channel.get<std::int64_t>());
        }
        result["rgb_color"] = std::move(channels);
    }
    if (result.contains("rgb_color") && result.contains("color_temp_kelvin"))
    {
        home_error("home lighting preset targets cannot mix rgb_color and color_temp_kelvin");
    }
    if (state == "off" && result.size() > 1)
    {
        home_error("home lighting preset off targets cannot set brightness or color");
    }
    return result;
}

Json load_presets(const Json &input, const HomeSettings &settings)
{
    if (!input.is_object() || input.size() > 100)
    {
        home_error("home lighting_presets must be an object with at most 100 presets");
    }
    Json result = Json::object();
    for (const auto &[name, preset] : input.items())
    {
        if (!slug(name))
        {
            home_error("home lighting preset names must be lowercase names of at most 64 characters");
        }
        check_object(preset, {"targets"}, {"targets"});
        const auto &targets = preset.at("targets");
        if (!targets.is_object() || targets.empty() || targets.size() > PRESET_TARGETS)
        {
            home_error("home lighting preset targets must map 1 to 16 reviewed lights to their states");
        }
        Json normalized = Json::object();
        for (const auto &[identifier, state] : targets.items())
        {
            if (!includes(settings.reviewed.at("lights"), identifier))
            {
                home_error("home lighting preset targets must be exact reviewed lights");
            }
            normalized[identifier] = preset_target(state);
        }
        const auto fingerprint = hash({{"component", "home_lighting_preset"}, {"native_preset_version", 1}, {"targets", normalized}});
        result[name] = {{"fingerprint", fingerprint}, {"targets", std::move(normalized)}};
    }
    return result;
}

HomeSettings load_home(const Json &input)
{
    check_object(
        input, {"base_url", "token", "allow_http", "expose", "deny_domains", "lights", "rooms", "climate", "fans", "media_players", "lighting_presets", "poll"},
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
                         {"media_players", Json::object()},
                         {"lighting_presets", Json::object()}};
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
    settings.reviewed["lighting_presets"] = load_presets(input.value("lighting_presets", Json::object()), settings);
    settings.poll = load_poll(input.value("poll", Json::object()), settings);
    settings.binding = hash({{"component", "home"},
                             {"native_binding_version", 2},
                             {"base_url", settings.base_url},
                             {"allow_http", settings.allow_http},
                             {"reviewed", settings.reviewed}});
    settings.source = hash({{"component", "home_source"}, {"base_url", settings.base_url}});
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
    return raw.at("state") != Json("unknown") && raw.at("state") != Json("unavailable");
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

Json missing_observation(const std::string &identifier, const std::string &observed_at)
{
    return {{"entity_id", identifier},  {"state", nullptr},        {"attributes", Json::object()}, {"availability", "missing"},
            {"reported_leaf", nullptr}, {"last_changed", nullptr}, {"last_updated", nullptr},      {"observed_at", observed_at}};
}

Json bounded_snapshot(Json snapshot)
{
    // Two snapshots plus the envelope must fit the core's 20,000-byte event payload bound.
    if (snapshot.dump().size() > SNAPSHOT_BYTES)
    {
        snapshot["attributes"] = Json::object();
        snapshot["attributes_omitted"] = true;
    }
    return snapshot;
}

Json comparable(const Json &snapshot)
{
    return {{"state", snapshot.value("state", Json())},
            {"attributes", snapshot.value("attributes", Json::object())},
            {"attributes_omitted", snapshot.value("attributes_omitted", false)},
            {"availability", snapshot.value("availability", Json())},
            {"reported_leaf", snapshot.value("reported_leaf", Json())}};
}

bool sensor_domain(const std::string &identifier)
{
    const auto domain = domain_of(identifier);
    return domain == "sensor" || domain == "binary_sensor";
}

// Parses all of text as a decimal number in the general format of std::from_chars. Apple's libc++ lacks the
// floating-point from_chars, so there the same grammar is checked here and the classic locale converts the text.
// That path refuses infinity and NaN spellings, which callers reject as non-finite anyway, and subnormal values where
// strtod reports them as range errors. The stream rethrows an allocation failure instead of reporting a parse failure.
bool parse_double(const std::string &text, double &value)
{
#if defined(__cpp_lib_to_chars)
    const auto *end = text.data() + text.size();
    const auto [last, failure] = std::from_chars(text.data(), end, value);
    return failure == std::errc() && last == end;
#else
    std::size_t index = text.starts_with('-') ? 1 : 0;
    const auto digits = [&]
    {
        const auto start = index;
        while (index < text.size() && std::isdigit(static_cast<unsigned char>(text[index])) != 0)
        {
            ++index;
        }
        return index - start;
    };
    auto mantissa = digits();
    if (index < text.size() && text[index] == '.')
    {
        ++index;
        mantissa += digits();
    }
    if (mantissa == 0)
    {
        return false;
    }
    if (index < text.size() && (text[index] == 'e' || text[index] == 'E'))
    {
        ++index;
        if (index < text.size() && (text[index] == '+' || text[index] == '-'))
        {
            ++index;
        }
        if (digits() == 0)
        {
            return false;
        }
    }
    if (index != text.size())
    {
        return false;
    }
    std::istringstream stream(text);
    stream.imbue(std::locale::classic());
    stream.exceptions(std::ios_base::badbit);
    stream >> value;
    return !stream.fail() && stream.eof();
#endif
}

std::optional<double> numeric_state(const Json &state)
{
    if (!state.is_string() || state.get_ref<const std::string &>().empty())
    {
        return std::nullopt;
    }
    const auto &text = state.get_ref<const std::string &>();
    double value = 0;
    if (!parse_double(text, value) || !std::isfinite(value))
    {
        return std::nullopt;
    }
    return value == 0 ? 0.0 : value;
}

std::optional<Json> sensor_reading(const Json &snapshot, const bool source_failed)
{
    static const std::string degree = "\xc2\xb0";
    static const std::map<std::string, std::pair<std::string, std::string>> binary{{"battery", {"battery", "low_battery"}},
                                                                                   {"moisture", {"leak", "moisture_detected"}},
                                                                                   {"motion", {"motion", "motion_detected"}},
                                                                                   {"occupancy", {"motion", "occupied"}}};
    static const std::map<std::string, std::set<std::string>> units{
        {"temperature", {degree + "C", degree + "F", "K"}},
        {"humidity", {"%"}},
        {"battery", {"%"}},
        {"power", {"mW", "W", "kW", "MW", "GW", "TW"}},
        {"energy", {"J", "kJ", "MJ", "GJ", "mWh", "Wh", "kWh", "MWh", "GWh", "TWh", "cal", "kcal", "Mcal", "Gcal", "thm"}}};
    const auto identifier = snapshot.at("entity_id").get<std::string>();
    const auto attributes = snapshot.value("attributes", Json::object());
    const auto device_class = attributes.value("device_class", Json());
    if (!device_class.is_string())
    {
        return std::nullopt;
    }
    const auto state = snapshot.value("state", Json());
    Json unit = attributes.value("unit_of_measurement", Json());
    Json value;
    Json meaning;
    Json reason;
    std::string kind;
    if (domain_of(identifier) == "binary_sensor")
    {
        const auto found = binary.find(device_class.get<std::string>());
        if (found == binary.end())
        {
            return std::nullopt;
        }
        kind = found->second.first;
        meaning = found->second.second;
        unit = nullptr;
        if (state == Json("on") || state == Json("off"))
        {
            value = state == Json("on");
        }
        else
        {
            reason = "no known binary state";
        }
    }
    else
    {
        const auto found = units.find(device_class.get<std::string>());
        if (found == units.end())
        {
            return std::nullopt;
        }
        kind = found->first;
        const auto numeric = numeric_state(state);
        if (!numeric)
        {
            reason = "no finite numeric reading";
        }
        else
        {
            value = *numeric;
            if (!unit.is_string() || !found->second.contains(unit.get<std::string>()))
            {
                reason = "unit missing or unrecognized; no conversion attempted";
            }
            else if ((kind == "battery" || kind == "humidity") && (*numeric < 0 || *numeric > 100))
            {
                reason = "reported percentage is outside 0 to 100";
            }
        }
    }
    const auto availability = snapshot.value("availability", Json());
    const bool current = availability == Json("available");
    const bool is_stale = snapshot.value("stale", true);
    return Json{{"entity_id", identifier},
                {"kind", kind},
                {"device_class", device_class},
                {"state_class", attributes.value("state_class", Json())},
                {"state", state},
                {"value", current ? value : Json()},
                {"unit", unit},
                {"meaning", meaning},
                {"availability", availability},
                {"stale", is_stale},
                {"usable", current && !is_stale && !source_failed && reason.is_null()},
                {"reason", reason},
                {"observed_at", snapshot.value("observed_at", Json())},
                {"last_updated", snapshot.value("last_updated", Json())}};
}

std::string failure_code(const std::exception &error)
{
    const auto *typed = dynamic_cast<const Error *>(&error);
    return typed != nullptr && slug(typed->code(), 32) ? typed->code() : std::string("internal");
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
        Transaction transaction(context_.db);
        context_.db.execute("CREATE TABLE IF NOT EXISTS native_home_snapshots ("
                            "source TEXT NOT NULL CHECK(length(source) = 64), "
                            "entity_id TEXT NOT NULL CHECK(length(CAST(entity_id AS BLOB)) BETWEEN 3 AND 255), "
                            "snapshot_json TEXT NOT NULL, observed_at INTEGER NOT NULL, "
                            "revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 9007199254740991), "
                            "PRIMARY KEY (source, entity_id))");
        context_.db.execute("CREATE TABLE IF NOT EXISTS native_home_poll_status ("
                            "source TEXT PRIMARY KEY CHECK(length(source) = 64), "
                            "attempted_at INTEGER NOT NULL, succeeded_at INTEGER, error TEXT, "
                            "consecutive_failures INTEGER NOT NULL CHECK(consecutive_failures BETWEEN 0 AND 9007199254740991))");
        transaction.commit();
    }

    [[nodiscard]] bool polling() const
    {
        return settings_.poll.enabled;
    }

    [[nodiscard]] std::int64_t poll_interval() const
    {
        return settings_.poll.interval_seconds;
    }

    [[nodiscard]] Json action_schema(const std::string &name) const
    {
        const auto table_name = name == "home_lights" ? "lights" : (name == "home_climate" ? "climate" : (name == "home_fan" ? "fans" : "media_players"));
        const Json allowed = name == "home_lights" ? settings_.reviewed.at(table_name) : keys(settings_.reviewed.at(table_name));
        Json properties = {{"binding", binding_schema()}, {"room", room_schema()}};
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

    [[nodiscard]] Json summary_schema() const
    {
        Json sensors = Json::array();
        for (const auto &identifier : visible_entities())
        {
            if (sensor_domain(identifier.get<std::string>()))
            {
                sensors.push_back(identifier);
            }
        }
        Json kinds = Json::array();
        for (const auto *kind : SENSOR_KINDS)
        {
            kinds.push_back(kind);
        }
        return object_schema({{"entity_id", identifier_schema(sensors)},
                              {"room", room_schema()},
                              {"source", {{"type", "string"}, {"enum", {"live", "cached"}}}},
                              {"kind", {{"type", "string"}, {"enum", kinds}}},
                              {"limit", {{"type", "integer"}, {"minimum", 1}, {"maximum", 200}}}});
    }

    [[nodiscard]] Json preset_schema() const
    {
        const auto &presets = settings_.reviewed.at("lighting_presets");
        const auto names = keys(presets);
        Json fingerprints = Json::array();
        for (const auto &[name, preset] : presets.items())
        {
            static_cast<void>(name);
            fingerprints.push_back(preset.at("fingerprint"));
        }
        return object_schema(
            {{"binding", binding_schema()},
             {"name",
              {{"type", "string"}, {"minLength", 1}, {"maxLength", 64}, {"pattern", "^[a-z0-9_]{1,64}$"}, {"enum", names.empty() ? Json::array({""}) : names}}},
             {"fingerprint",
              {{"type", "string"},
               {"minLength", 64},
               {"maxLength", 64},
               {"pattern", "^[0-9a-f]{64}$"},
               {"enum", fingerprints.empty() ? Json::array({""}) : fingerprints}}}},
            {"binding", "name", "fingerprint"});
    }

    [[nodiscard]] Json targets(const Json &arguments) const
    {
        validate(arguments, object_schema(Json::object()));
        auto result = settings_.reviewed;
        result["binding"] = settings_.binding;
        result["room_targeting"] = "explicit entities only; room restricts targets and never expands them";
        result["capabilities_source"] = "home_capabilities reads reported state attributes; execution rechecks them";
        result["preset_execution"] = "every target is preflighted, then one light request per target in entity order; "
                                     "a rejected or unknown submission stops the rest; never scenes or scripts";
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
            if (arguments.contains("domain") && arguments.at("domain") != Json(domain_of(identifier)))
            {
                home_error("home entity does not belong to the requested domain");
            }
            selected.push_back(read_state(identifier));
        }
        else
        {
            for (const auto &raw : read_all_states())
            {
                const auto identifier = raw.at("entity_id").get<std::string>();
                if (!settings_.visible(identifier) ||
                    (arguments.contains("room") && !includes(settings_.reviewed.at("rooms").at(arguments.at("room").get<std::string>()), identifier)) ||
                    (arguments.contains("domain") && arguments.at("domain") != Json(domain_of(identifier))))
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
        require_binding(arguments);
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
        return submit(plan, submission(plan));
    }

    [[nodiscard]] Json lighting_preset(const Json &arguments)
    {
        require_binding(arguments);
        const auto &presets = settings_.reviewed.at("lighting_presets");
        if (arguments.is_object() && arguments.contains("name") && arguments.at("name").is_string() && arguments.contains("fingerprint") &&
            arguments.at("fingerprint").is_string())
        {
            const auto requested = arguments.at("name").get<std::string>();
            if (!presets.contains(requested) || presets.at(requested).at("fingerprint") != arguments.at("fingerprint"))
            {
                throw Error("home_preset", "the lighting preset is missing or changed; read home_targets before requesting it again");
            }
        }
        validate(arguments, preset_schema());
        const auto name = arguments.at("name").get<std::string>();
        const auto &preset = presets.at(name);
        std::vector<HomePlan> plans;
        std::vector<HttpRequest> inputs;
        for (const auto &[identifier, state] : preset.at("targets").items())
        {
            auto target = state;
            target["entity_ids"] = Json::array({identifier});
            plans.push_back(light_plan(target));
            inputs.push_back(submission(plans.back()));
        }
        Json results = Json::array();
        Json stop_reason;
        std::size_t accepted = 0;
        bool unknown = false;
        bool readback = false;
        bool matched = true;
        std::size_t index = 0;
        for (; index < plans.size(); ++index)
        {
            const auto limits = current_execution_limits();
            if (index > 0 && limits && (limits->cancelled() || limits->expired()))
            {
                stop_reason = limits->cancelled() ? "cancelled" : "deadline";
                break;
            }
            auto result = submit(plans[index], inputs[index]);
            const auto request_status = result.at("request_status").get<std::string>();
            accepted += request_status == "accepted" ? 1 : 0;
            unknown = unknown || request_status == "unknown";
            readback = readback || result.contains("confirmed");
            matched = matched && result.value("confirmed", false);
            results.push_back(std::move(result));
            if (request_status != "accepted")
            {
                stop_reason = request_status;
                ++index;
                break;
            }
        }
        Json not_attempted = Json::array();
        for (; index < plans.size(); ++index)
        {
            not_attempted.push_back(plans[index].entities.front());
        }
        const bool confirmed = readback && matched && not_attempted.empty();
        const bool rejected = accepted == 0 && stop_reason == Json("rejected");
        Json report = {{"name", name},
                       {"fingerprint", preset.at("fingerprint")},
                       {"entity_ids", keys(preset.at("targets"))},
                       {"status", rejected ? "rejected" : (confirmed ? "observed" : "requested_unconfirmed")},
                       {"partial", accepted > 0 && accepted < plans.size()},
                       {"results", results},
                       {"not_attempted", not_attempted},
                       {"stop_reason", stop_reason},
                       {"atomic", false},
                       {"physical_delivery_verified", false},
                       {"confirmation_scope", "reported requested state of each submitted light; not physical completion or causal proof"},
                       {"retry_safe", false}};
        if (readback)
        {
            report["confirmed"] = confirmed;
        }
        if (unknown)
        {
            report["effect_unknown"] = true;
        }
        return report;
    }

    [[nodiscard]] std::string preset_summary(const Json &arguments) const
    {
        const auto &presets = settings_.reviewed.at("lighting_presets");
        std::string targets = "{}";
        if (arguments.is_object() && arguments.contains("name") && arguments.at("name").is_string() &&
            presets.contains(arguments.at("name").get<std::string>()))
        {
            targets = presets.at(arguments.at("name").get<std::string>()).at("targets").dump();
        }
        return "home_lighting_preset " + arguments.dump() + " targets " + targets;
    }

    [[nodiscard]] Json poll_status(const Json &arguments) const
    {
        validate(arguments, object_schema(Json::object()));
        return poll_report(context_.now());
    }

    [[nodiscard]] Json poll(const Json &arguments)
    {
        validate(arguments, object_schema(Json::object()));
        return sample();
    }

    void poll_job()
    {
        static_cast<void>(sample());
    }

    [[nodiscard]] Json sensor_summary(const Json &arguments) const
    {
        validate(arguments, summary_schema());
        std::optional<std::string> requested;
        if (arguments.contains("entity_id"))
        {
            requested = arguments.at("entity_id").get<std::string>();
            require_scope(*requested, arguments);
            if (!sensor_domain(*requested))
            {
                home_error("home summary target is not an exposed sensor or binary sensor");
            }
        }
        const auto members = arguments.contains("room") ? settings_.reviewed.at("rooms").at(arguments.at("room").get<std::string>()) : Json();
        const auto source = arguments.value("source", std::string("live"));
        Json source_error;
        std::vector<Json> snapshots;
        if (source == "cached")
        {
            if (!settings_.poll.enabled)
            {
                home_error("cached home summaries require opt-in polling");
            }
            if (requested && !std::binary_search(settings_.poll.entities.begin(), settings_.poll.entities.end(), *requested))
            {
                home_error("the requested sensor is not in the polling allowlist");
            }
            const auto report = poll_report(context_.now());
            source_error = report.at("error");
            snapshots.assign(report.at("entities").begin(), report.at("entities").end());
        }
        else
        {
            const auto observed_at = context_.timestamp();
            const auto reported = requested ? std::vector<Json>{read_state(*requested)} : read_all_states();
            for (const auto &raw : reported)
            {
                const auto identifier = raw.at("entity_id").get<std::string>();
                if (settings_.visible(identifier) && sensor_domain(identifier))
                {
                    auto snapshot = observation(raw, observed_at);
                    snapshot["stale"] = false;
                    snapshots.push_back(std::move(snapshot));
                }
            }
        }
        std::sort(snapshots.begin(), snapshots.end(),
                  [](const Json &left, const Json &right)
                  {
                      return left.at("entity_id").get<std::string>() < right.at("entity_id").get<std::string>();
                  });
        std::vector<Json> selected;
        std::vector<Json> unclassified;
        for (const auto &snapshot : snapshots)
        {
            const auto identifier = snapshot.at("entity_id").get<std::string>();
            if (!sensor_domain(identifier) || (requested && identifier != *requested) || (!members.is_null() && !includes(members, identifier)))
            {
                continue;
            }
            auto reading = sensor_reading(snapshot, !source_error.is_null());
            if (!reading)
            {
                unclassified.push_back({{"entity_id", identifier},
                                        {"availability", snapshot.value("availability", Json())},
                                        {"stale", snapshot.value("stale", true)},
                                        {"reason", "no supported device_class was reported; no capability was inferred"}});
            }
            else if (!arguments.contains("kind") || reading->at("kind") == arguments.at("kind"))
            {
                selected.push_back(std::move(*reading));
            }
        }
        const auto limit = static_cast<std::size_t>(integer_field(arguments, "limit", 1, 200, 100));
        Json readings = Json::object();
        for (const auto *kind : SENSOR_KINDS)
        {
            readings[kind] = Json::array();
        }
        const auto shown = std::min(limit, selected.size());
        for (std::size_t index = 0; index < shown; ++index)
        {
            readings[selected[index].at("kind").get<std::string>()].push_back(selected[index]);
        }
        Json others = Json::array();
        for (std::size_t index = 0; index < unclassified.size() && shown + index < limit; ++index)
        {
            others.push_back(unclassified[index]);
        }
        return {{"source", source},
                {"readings", readings},
                {"unclassified", others},
                {"truncated", selected.size() + unclassified.size() > limit},
                {"source_error", source_error},
                {"units", "as reported by Home Assistant; no conversion"},
                {"freshness_basis", "time of local state reads, not proof of device freshness"},
                {"energy_scope", "reported instantaneous power and meter readings; no totals, costs, or conversions"}};
    }

private:
    [[nodiscard]] Json binding_schema() const
    {
        return {{"type", "string"}, {"minLength", 64}, {"maxLength", 64}, {"pattern", "^[0-9a-f]{64}$"}, {"enum", Json::array({settings_.binding})}};
    }

    void require_binding(const Json &arguments) const
    {
        if (arguments.is_object() && arguments.contains("binding") && arguments.at("binding").is_string() && arguments.at("binding") != settings_.binding)
        {
            throw Error("home_binding", "home configuration changed; read home_targets and request new approval");
        }
    }

    [[nodiscard]] Json sample()
    {
        if (!settings_.poll.enabled)
        {
            home_error("home polling is disabled; enable poll with exact exposed entities first");
        }
        const auto attempted_at = context_.now();
        try
        {
            const auto reported = read_all_states();
            const auto observed_at = context_.now();
            const auto rendered = timestamp(observed_at);
            std::map<std::string, Json> polled;
            for (const auto &raw : reported)
            {
                const auto identifier = raw.at("entity_id").get<std::string>();
                if (std::binary_search(settings_.poll.entities.begin(), settings_.poll.entities.end(), identifier))
                {
                    polled.emplace(identifier, bounded_snapshot(observation(raw, rendered)));
                }
            }
            std::vector<Json> snapshots;
            for (const auto &identifier : settings_.poll.entities)
            {
                const auto found = polled.find(identifier);
                snapshots.push_back(found == polled.end() ? missing_observation(identifier, rendered) : found->second);
            }
            return record_sample(snapshots, attempted_at, observed_at);
        }
        catch (const std::exception &error)
        {
            record_failure(attempted_at, failure_code(error));
            throw Error("home_poll", "home polling failed; cached snapshots were retained; inspect home_poll_status");
        }
    }

    [[nodiscard]] Json record_sample(const std::vector<Json> &snapshots, const std::int64_t attempted_at, const std::int64_t observed_at)
    {
        std::int64_t baselined = 0;
        std::int64_t changed = 0;
        Transaction transaction(context_.db);
        const auto health = context_.db.query("SELECT attempted_at FROM native_home_poll_status WHERE source = ?", {settings_.source});
        const bool older = !health.empty() && stored_integer(health.front().at("attempted_at")) > attempted_at;
        for (std::size_t index = 0; !older && index < snapshots.size(); ++index)
        {
            const auto &snapshot = snapshots[index];
            const auto identifier = snapshot.at("entity_id").get<std::string>();
            const auto rows = context_.db.query("SELECT snapshot_json, revision FROM native_home_snapshots WHERE source = ? AND entity_id = ?",
                                                {settings_.source, identifier});
            std::int64_t revision = 1;
            if (rows.empty())
            {
                ++baselined;
            }
            else
            {
                const auto previous = stored_snapshot(rows.front(), identifier);
                revision = stored_revision(rows.front());
                if (comparable(previous) != comparable(snapshot))
                {
                    if (revision >= MAX_JSON_INTEGER)
                    {
                        throw Error("home_cache", "home snapshot revision is exhausted");
                    }
                    ++revision;
                    context_.emit("home", "home.state_changed",
                                  {{"entity_id", identifier},
                                   {"previous", previous},
                                   {"current", snapshot},
                                   {"observed_at", timestamp(observed_at)},
                                   {"revision", revision},
                                   {"source", settings_.source},
                                   {"history_complete", false}},
                                  "native:home:" + settings_.source + ":" + identifier + ":" + std::to_string(revision));
                    ++changed;
                }
            }
            context_.db.execute("INSERT INTO native_home_snapshots (source, entity_id, snapshot_json, observed_at, revision) VALUES (?, ?, ?, ?, ?) "
                                "ON CONFLICT(source, entity_id) DO UPDATE SET snapshot_json = excluded.snapshot_json, "
                                "observed_at = excluded.observed_at, revision = excluded.revision",
                                {settings_.source, identifier, snapshot.dump(), observed_at, revision});
        }
        if (!older)
        {
            context_.db.execute("INSERT INTO native_home_poll_status (source, attempted_at, succeeded_at, error, consecutive_failures) "
                                "VALUES (?, ?, ?, NULL, 0) ON CONFLICT(source) DO UPDATE SET attempted_at = excluded.attempted_at, "
                                "succeeded_at = excluded.succeeded_at, error = NULL, consecutive_failures = 0",
                                {settings_.source, attempted_at, observed_at});
        }
        transaction.commit();
        return {{"entities", snapshots.size()}, {"baselined", baselined},        {"changed", changed},       {"observed_at", timestamp(observed_at)},
                {"source", settings_.source},   {"ignored_older_sample", older}, {"history_complete", false}};
    }

    void record_failure(const std::int64_t attempted_at, const std::string &code) noexcept
    {
        try
        {
            Transaction transaction(context_.db);
            context_.db.execute("INSERT INTO native_home_poll_status (source, attempted_at, succeeded_at, error, consecutive_failures) "
                                "VALUES (?, ?, NULL, ?, 1) ON CONFLICT(source) DO UPDATE SET attempted_at = excluded.attempted_at, "
                                "error = excluded.error, consecutive_failures = MIN(native_home_poll_status.consecutive_failures + 1, 9007199254740991) "
                                "WHERE excluded.attempted_at >= native_home_poll_status.attempted_at",
                                {settings_.source, attempted_at, code});
            transaction.commit();
        }
        catch (const std::exception &)
        {
            std::fputs("ERROR: native home polling health could not be recorded\n", stderr);
        }
    }

    [[nodiscard]] Json poll_report(const std::int64_t moment) const
    {
        const auto health = context_.db.query("SELECT attempted_at, succeeded_at, error, consecutive_failures FROM native_home_poll_status WHERE source = ?",
                                              {settings_.source});
        std::optional<std::int64_t> attempted;
        std::optional<std::int64_t> succeeded;
        Json error;
        std::int64_t failures = 0;
        if (!health.empty())
        {
            const auto &row = health.front();
            attempted = stored_integer(row.at("attempted_at"));
            if (!row.at("succeeded_at").is_null())
            {
                succeeded = stored_integer(row.at("succeeded_at"));
            }
            if (!row.at("error").is_null())
            {
                error = row.at("error").is_string() && slug(row.at("error").get<std::string>(), 32) ? row.at("error") : Json("internal");
            }
            failures = stored_integer(row.at("consecutive_failures"));
        }
        Json entities = Json::array();
        for (const auto &identifier : settings_.poll.entities)
        {
            const auto rows = context_.db.query("SELECT snapshot_json, observed_at, revision FROM native_home_snapshots WHERE source = ? AND entity_id = ?",
                                                {settings_.source, identifier});
            if (rows.empty())
            {
                entities.push_back({{"entity_id", identifier}, {"availability", "not_observed"}, {"revision", 0}, {"stale", true}});
                continue;
            }
            auto snapshot = stored_snapshot(rows.front(), identifier);
            snapshot["revision"] = stored_revision(rows.front());
            snapshot["stale"] = stale(stored_integer(rows.front().at("observed_at")), moment);
            entities.push_back(std::move(snapshot));
        }
        return {{"enabled", settings_.poll.enabled},
                {"source", settings_.source},
                {"interval_seconds", settings_.poll.interval_seconds},
                {"stale_after_seconds", settings_.poll.stale_after_seconds},
                {"attempted_at", attempted ? Json(timestamp(*attempted)) : Json()},
                {"succeeded_at", succeeded ? Json(timestamp(*succeeded)) : Json()},
                {"error", error},
                {"consecutive_failures", failures},
                {"stale", !succeeded || stale(*succeeded, moment)},
                {"entities", entities},
                {"history_complete", false},
                {"freshness_basis", "time of local state reads, not proof of device freshness"}};
    }

    [[nodiscard]] bool stale(const std::int64_t observed_at, const std::int64_t moment) const
    {
        return moment < observed_at || moment - observed_at > settings_.poll.stale_after_seconds;
    }

    [[nodiscard]] static std::int64_t stored_integer(const Json &value)
    {
        if (!value.is_number_integer() || value.get<double>() < 0 || value.get<double>() > static_cast<double>(MAX_JSON_INTEGER))
        {
            throw Error("home_cache", "home polling state is malformed");
        }
        return value.get<std::int64_t>();
    }

    [[nodiscard]] static std::int64_t stored_revision(const Json &row)
    {
        const auto revision = stored_integer(row.at("revision"));
        if (revision < 1)
        {
            throw Error("home_cache", "home snapshot revision is malformed");
        }
        return revision;
    }

    [[nodiscard]] static Json stored_snapshot(const Json &row, const std::string &identifier)
    {
        Json snapshot;
        try
        {
            snapshot = parse_json(row.at("snapshot_json").get<std::string>(), SNAPSHOT_BYTES);
        }
        catch (const std::exception &)
        {
            throw Error("home_cache", "the stored home snapshot is unreadable");
        }
        if (!snapshot.is_object() || snapshot.value("entity_id", Json()) != identifier || !snapshot.value("attributes", Json()).is_object())
        {
            throw Error("home_cache", "the stored home snapshot is malformed");
        }
        return snapshot;
    }

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

    [[nodiscard]] std::vector<Json> read_all_states() const
    {
        const auto response = get("/api/states");
        if (!response.is_array() || response.size() > 10000)
        {
            home_error("Home Assistant returned an invalid or excessive states list");
        }
        std::vector<Json> result;
        std::set<std::string> seen;
        for (const auto &raw : response)
        {
            check_state(raw);
            if (!seen.insert(raw.at("entity_id").get<std::string>()).second)
            {
                home_error("Home Assistant returned duplicate states");
            }
            result.push_back(raw);
        }
        return result;
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
        plan.service = arguments.at("state") == Json("on") ? "turn_on" : "turn_off";
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
        if ((arguments.at("state") == Json("off") &&
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

    // Prepared before any effect, so a credential failure cannot follow an earlier preset submission.
    [[nodiscard]] HttpRequest submission(const HomePlan &plan) const
    {
        auto input = request("POST", "/api/services/" + plan.domain + "/" + plan.service);
        Json body = plan.data;
        body["entity_id"] = plan.entities.size() == 1 ? Json(plan.entities.front()) : Json(plan.entities);
        input.body = body.dump();
        return input;
    }

    [[nodiscard]] Json submit(const HomePlan &plan, const HttpRequest &input) const
    {
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
        Json result = {{"called", plan.domain + "." + plan.service},
                       {"entity_ids", plan.entities},
                       {"request_status", request_status},
                       {"status", request_status == "rejected" ? "rejected" : (confirmed ? "observed" : "requested_unconfirmed")},
                       {"physical_delivery_verified", false},
                       {"confirmation_scope", "reported requested state only; not physical completion or causal proof"},
                       {"observations", observations},
                       {"readback_errors", readback_errors},
                       {"error", error},
                       {"retry_safe", false}};
        if (request_status != "rejected")
        {
            result["confirmed"] = confirmed;
        }
        if (request_status == "unknown")
        {
            result["effect_unknown"] = true;
        }
        return result;
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
                  "Read exact reviewed home targets, room scopes, limits, lighting presets and current binding; offline.",
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
    registry.add({"home_sensor_summary",
                  "Classify exposed sensors by reported device_class with original units, availability and staleness; no conversions or totals.",
                  Tier::read,
                  home->summary_schema(),
                  [home](const Json &arguments)
                  {
                      return home->sensor_summary(arguments);
                  },
                  {}});
    registry.add({"home_poll_status",
                  "Read local polling health, cached minimized snapshots and staleness; no network.",
                  Tier::read,
                  object_schema(Json::object()),
                  [home](const Json &arguments)
                  {
                      return home->poll_status(arguments);
                  },
                  {}});
    registry.add({"home_poll",
                  "Sample configured entities once and queue changed snapshots as events; first samples are silent; refused when polling is disabled.",
                  Tier::read,
                  object_schema(Json::object()),
                  [home](const Json &arguments)
                  {
                      return home->poll(arguments);
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
    registry.add({"home_lighting_preset",
                  "Apply one reviewed lighting preset pinned by binding and fingerprint as individual light requests; no scenes or scripts; not atomic.",
                  Tier::act, home->preset_schema(),
                  [home](const Json &arguments)
                  {
                      return home->lighting_preset(arguments);
                  },
                  [home](const Json &arguments)
                  {
                      return home->preset_summary(arguments);
                  }});
    if (home->polling())
    {
        context.add_job("home.poll", home->poll_interval(),
                        [home]()
                        {
                            home->poll_job();
                        });
    }
}
}  // namespace switchboard

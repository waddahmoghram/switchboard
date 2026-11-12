#include "switchboard/native.hpp"

#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <memory>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <arpa/inet.h>

namespace switchboard
{
namespace
{
constexpr std::int64_t MAX_DEVICES = 16;
constexpr std::int64_t MAX_TRANSITION = 100;
constexpr std::size_t SHELLY_BYTES = 65536;

[[noreturn]] void reject(const std::string &code)
{
    static const std::map<std::string, std::string> messages = {{"config", "Invalid native device configuration; consult native/docs/devices.md"},
                                                                {"arguments", "Invalid fixed-device arguments"},
                                                                {"binding", "Device binding changed; list devices and obtain a new approval"},
                                                                {"scope", "Device control or the requested scope is not enabled"},
                                                                {"safety", "Device safety preflight refused the operation"},
                                                                {"identity", "Shelly identity, model or generation does not match"},
                                                                {"auth", "Device authentication is absent, refused or unsupported"},
                                                                {"secret", "The configured device credential is unavailable or invalid"},
                                                                {"transport", "Device transport failed; delivery may be unknown; no automatic replay"},
                                                                {"redirect", "Device redirect refused; no automatic replay"},
                                                                {"http", "Device returned an unsuccessful web status; response withheld"},
                                                                {"protocol", "Device returned an invalid or oversized response; details withheld"},
                                                                {"provider", "Device rejected the fixed operation; details withheld"}};
    throw Error("devices_" + code, messages.at(code));
}

bool printable(const std::string &text)
{
    return std::all_of(text.begin(), text.end(),
                       [](const unsigned char character)
                       {
                           return character >= 32 && character <= 126;
                       });
}

bool name_valid(const std::string &text)
{
    return !text.empty() && text.size() <= 32 && text.front() >= 'a' && text.front() <= 'z' &&
           std::all_of(text.begin(), text.end(),
                       [](const unsigned char character)
                       {
                           return (character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') || character == '_';
                       });
}

bool digest_valid(const std::string &text)
{
    return text.size() == 64 && std::all_of(text.begin(), text.end(),
                                            [](const unsigned char character)
                                            {
                                                return (character >= '0' && character <= '9') || (character >= 'a' && character <= 'f');
                                            });
}

bool integer_in(const Json &value, const std::int64_t minimum, const std::int64_t maximum)
{
    if (!value.is_number_integer())
    {
        return false;
    }
    if (value.is_number_unsigned())
    {
        const auto number = value.get<std::uint64_t>();
        return number <= static_cast<std::uint64_t>(maximum) && (minimum <= 0 || number >= static_cast<std::uint64_t>(minimum));
    }
    const auto number = value.get<std::int64_t>();
    return number >= minimum && number <= maximum;
}

Json optional_integer(const Json &object, const std::string &key, const std::int64_t minimum, const std::int64_t maximum)
{
    return object.contains(key) && integer_in(object.at(key), minimum, maximum) ? object.at(key) : Json();
}

Json optional_boolean(const Json &object, const std::string &key)
{
    return object.contains(key) && object.at(key).is_boolean() ? object.at(key) : Json();
}

std::int64_t required_integer(const Json &object, const std::string &key, const std::int64_t minimum, const std::int64_t maximum)
{
    if (!object.contains(key) || !integer_in(object.at(key), minimum, maximum))
    {
        reject("arguments");
    }
    return object.at(key).get<std::int64_t>();
}

std::string lowercase(std::string text)
{
    std::transform(text.begin(), text.end(), text.begin(),
                   [](const unsigned char character)
                   {
                       return character >= 'A' && character <= 'Z' ? static_cast<char>(character + ('a' - 'A')) : static_cast<char>(character);
                   });
    return text;
}

bool hostname_valid(const std::string &host)
{
    if (host.empty() || host.size() > 253)
    {
        return false;
    }
    std::size_t offset = 0;
    while (offset < host.size())
    {
        const auto end = host.find('.', offset);
        const auto label = host.substr(offset, end == std::string::npos ? end : end - offset);
        const auto alphanumeric = [](const unsigned char character)
        {
            return (character >= 'a' && character <= 'z') || (character >= '0' && character <= '9');
        };
        if (label.empty() || label.size() > 63 || !alphanumeric(label.front()) || !alphanumeric(label.back()) ||
            !std::all_of(label.begin(), label.end(),
                         [&](const unsigned char character)
                         {
                             return alphanumeric(character) || character == '-';
                         }))
        {
            return false;
        }
        if (end == std::string::npos)
        {
            return true;
        }
        offset = end + 1;
    }
    return false;
}

std::string origin(const std::string &input, const bool allow_http, const bool wled)
{
    if (input.empty() || input.size() > 300 || !printable(input) || input.find_first_of(" \\%?#@") != std::string::npos)
    {
        reject("config");
    }
    validate_endpoint(input, allow_http);
    const auto delimiter = input.find("://");
    if (delimiter == std::string::npos)
    {
        reject("config");
    }
    const auto scheme = input.substr(0, delimiter);
    if (scheme != "http" && scheme != "https")
    {
        reject("config");
    }
    auto authority = input.substr(delimiter + 3);
    if (authority.ends_with('/'))
    {
        authority.pop_back();
    }
    if (authority.empty() || authority.find('/') != std::string::npos)
    {
        reject("config");
    }
    std::string host;
    std::string port;
    if (authority.front() == '[')
    {
        const auto close = authority.find(']');
        if (close == std::string::npos)
        {
            reject("config");
        }
        host = authority.substr(1, close - 1);
        if (close + 1 < authority.size())
        {
            if (authority[close + 1] != ':')
            {
                reject("config");
            }
            port = authority.substr(close + 2);
            if (port.empty())
            {
                reject("config");
            }
        }
    }
    else
    {
        const auto colon = authority.find(':');
        host = lowercase(authority.substr(0, colon));
        if (colon != std::string::npos)
        {
            port = authority.substr(colon + 1);
            if (port.empty())
            {
                reject("config");
            }
        }
    }
    std::array<unsigned char, 16> address{};
    std::array<char, INET6_ADDRSTRLEN> normalized{};
    const bool ipv4 = inet_pton(AF_INET, host.c_str(), address.data()) == 1;
    const bool ipv6 = !ipv4 && inet_pton(AF_INET6, host.c_str(), address.data()) == 1;
    bool private_address = false;
    bool wled_address = false;
    if (ipv4)
    {
        private_address = address[0] == 10 || (address[0] == 172 && address[1] >= 16 && address[1] <= 31) || (address[0] == 192 && address[1] == 168);
        wled_address = private_address || address[0] == 127 || (address[0] == 169 && address[1] == 254);
    }
    else if (ipv6)
    {
        private_address = (address[0] & 0xFE) == 0xFC;
        const bool loopback = std::all_of(address.begin(), address.end() - 1,
                                          [](const unsigned char value)
                                          {
                                              return value == 0;
                                          }) &&
                              address.back() == 1;
        wled_address = private_address || loopback;
    }
    else if (!hostname_valid(host))
    {
        reject("config");
    }
    if (wled)
    {
        if (!wled_address && !(!ipv4 && !ipv6 && (host == "localhost" || host.ends_with(".local") || host.ends_with(".home.arpa"))))
        {
            reject("config");
        }
    }
    else if (scheme == "http" && !private_address)
    {
        reject("config");
    }
    if (ipv4 || ipv6)
    {
        if (inet_ntop(ipv4 ? AF_INET : AF_INET6, address.data(), normalized.data(), normalized.size()) == nullptr)
        {
            reject("config");
        }
        host = ipv6 ? "[" + std::string(normalized.data()) + "]" : std::string(normalized.data());
    }
    if (!port.empty())
    {
        unsigned int number = 0;
        const auto parsed = std::from_chars(port.data(), port.data() + port.size(), number);
        if (parsed.ec != std::errc() || parsed.ptr != port.data() + port.size() || number == 0 || number > 65535)
        {
            reject("config");
        }
        port = number == (scheme == "http" ? 80U : 443U) ? "" : ":" + std::to_string(number);
    }
    return scheme + "://" + host + port;
}

Json integer_schema(const std::int64_t minimum, const std::int64_t maximum)
{
    return {{"type", "integer"}, {"minimum", minimum}, {"maximum", maximum}};
}

Json text_schema(const std::size_t maximum)
{
    return {{"type", "string"}, {"minLength", 1}, {"maxLength", maximum}};
}

Json binding_schema(const Json &bindings)
{
    return {{"type", "string"}, {"minLength", 64}, {"maxLength", 64}, {"pattern", "^[0-9a-f]{64}$"}, {"enum", bindings}};
}

void add_tool(Registry &registry, std::string name, std::string description, const Tier tier, Json schema, std::function<Json(const Json &)> handler,
              std::function<std::string(const Json &)> summary = {})
{
    const auto checked = [schema, handler = std::move(handler)](const Json &arguments)
    {
        try
        {
            validate(arguments, schema);
        }
        catch (const std::exception &)
        {
            reject("arguments");
        }
        return handler(arguments);
    };
    registry.add({std::move(name), std::move(description), tier, std::move(schema), checked, std::move(summary)});
}

HttpRequest request_for(const std::string &url, const std::string &method, const Json *body, const std::int64_t timeout, const std::size_t maximum,
                        const bool allow_http)
{
    HttpRequest request;
    request.url = url;
    request.method = method;
    request.headers = {{"Accept", "application/json"}};
    request.timeout_seconds = timeout;
    request.max_bytes = maximum;
    request.allow_http = allow_http;
    if (body != nullptr)
    {
        request.headers["Content-Type"] = "application/json";
        request.body = body->dump(-1, ' ', true);
    }
    return request;
}

HttpResponse send(Context &context, const HttpRequest &request)
{
    HttpResponse response;
    try
    {
        response = context.http.request(request);
    }
    catch (const std::exception &)
    {
        reject("transport");
    }
    if (response.body.size() > request.max_bytes)
    {
        reject("protocol");
    }
    if (response.status >= 300 && response.status < 400)
    {
        reject("redirect");
    }
    return response;
}

// Write reports for the action journal and typed C outcomes: "confirmed" is true only for a matching software
// readback, and an "uncertain" status also declares effect_unknown. Neither proves a physical effect.
void mark_outcome(Json &result)
{
    result["confirmed"] = result.at("status") == "state_matched";
    if (result.at("status") == "uncertain")
    {
        result["effect_unknown"] = true;
    }
}

Json decode(const HttpResponse &response)
{
    if (response.status < 200 || response.status >= 300)
    {
        reject("http");
    }
    try
    {
        return response_json(response);
    }
    catch (const std::exception &)
    {
        reject("protocol");
    }
}

struct WledPreset
{
    std::string name;
    std::int64_t id;
    std::string sha256;
};

struct WledDevice
{
    std::string name;
    std::string base_url;
    bool allow_http;
    bool allow_control;
    bool allow_global;
    std::int64_t max_brightness;
    std::int64_t max_transition;
    std::vector<std::int64_t> segments;
    std::vector<WledPreset> presets;
};

struct WledSettings
{
    std::int64_t timeout;
    std::size_t maximum;
    std::vector<WledDevice> devices;
};

WledSettings wled_settings(const Json &settings)
{
    try
    {
        check_object(settings, {"devices", "timeout_seconds", "max_response_bytes"}, {"devices"});
        WledSettings result{integer_field(settings, "timeout_seconds", 1, 30, 5),
                            static_cast<std::size_t>(integer_field(settings, "max_response_bytes", 4096, 262144, 65536)),
                            {}};
        const auto &devices = settings.at("devices");
        if (!devices.is_array() || devices.empty() || devices.size() > MAX_DEVICES)
        {
            reject("config");
        }
        std::set<std::string> names;
        std::set<std::string> endpoints;
        for (const auto &raw : devices)
        {
            check_object(
                raw, {"name", "base_url", "allow_http", "allow_control", "allow_global", "segments", "presets", "max_brightness", "max_transition_deciseconds"},
                {"name", "base_url", "segments"});
            WledDevice device;
            device.name = string_field(raw, "name", 32, true, 1);
            device.allow_http = boolean_field(raw, "allow_http");
            device.allow_control = boolean_field(raw, "allow_control");
            device.allow_global = boolean_field(raw, "allow_global");
            device.base_url = origin(string_field(raw, "base_url", 300, true, 1), device.allow_http, true);
            device.max_brightness = integer_field(raw, "max_brightness", 1, 255, 128);
            device.max_transition = integer_field(raw, "max_transition_deciseconds", 0, MAX_TRANSITION, MAX_TRANSITION);
            if (!name_valid(device.name) || !names.insert(device.name).second || !endpoints.insert(device.base_url).second || !raw.at("segments").is_array() ||
                raw.at("segments").empty() || raw.at("segments").size() > 16)
            {
                reject("config");
            }
            std::set<std::int64_t> segments;
            for (const auto &segment : raw.at("segments"))
            {
                if (!integer_in(segment, 0, 63) || !segments.insert(segment.get<std::int64_t>()).second)
                {
                    reject("config");
                }
            }
            device.segments.assign(segments.begin(), segments.end());
            const auto presets = raw.value("presets", Json::array());
            if (!presets.is_array() || presets.size() > 16)
            {
                reject("config");
            }
            std::set<std::string> preset_names;
            std::set<std::int64_t> preset_ids;
            for (const auto &preset : presets)
            {
                check_object(preset, {"name", "id", "sha256"}, {"name", "id", "sha256"});
                WledPreset selected{string_field(preset, "name", 32, true, 1), required_integer(preset, "id", 1, 250),
                                    string_field(preset, "sha256", 64, true, 64)};
                if (!name_valid(selected.name) || !digest_valid(selected.sha256) || !preset_names.insert(selected.name).second ||
                    !preset_ids.insert(selected.id).second)
                {
                    reject("config");
                }
                device.presets.push_back(std::move(selected));
            }
            std::sort(device.presets.begin(), device.presets.end(),
                      [](const WledPreset &left, const WledPreset &right)
                      {
                          return left.name < right.name;
                      });
            result.devices.push_back(std::move(device));
        }
        return result;
    }
    catch (const std::exception &)
    {
        reject("config");
    }
}

Json wled_configuration(const WledSettings &settings, const WledDevice &device)
{
    Json presets = Json::array();
    for (const auto &preset : device.presets)
    {
        presets.push_back({{"name", preset.name}, {"id", preset.id}, {"sha256", preset.sha256}});
    }
    return {{"engine", "switchboard_cpp_wled_static_v1"},
            {"device", device.name},
            {"base_url", device.base_url},
            {"allow_http", device.allow_http},
            {"allow_control", device.allow_control},
            {"allow_global", device.allow_global},
            {"segments", device.segments},
            {"presets", presets},
            {"max_brightness", device.max_brightness},
            {"max_transition_deciseconds", device.max_transition},
            {"timeout_seconds", settings.timeout},
            {"max_response_bytes", settings.maximum},
            {"max_segments", 64},
            {"suppress_notification", true}};
}

std::string wled_binding(const WledSettings &settings, const WledDevice &device, const Json &scope)
{
    return hash({{"configuration", wled_configuration(settings, device)}, {"scope", scope}});
}

Json global_scope()
{
    return {{"kind", "global"}};
}

Json segment_scope(const std::int64_t id)
{
    return {{"kind", "segment"}, {"segment", id}};
}

Json preset_scope(const WledPreset &preset)
{
    return {{"kind", "preset"}, {"name", preset.name}, {"id", preset.id}, {"sha256", preset.sha256}};
}

const WledDevice &wled_device(const WledSettings &settings, const Json &arguments)
{
    const auto name = string_field(arguments, "device", 32, true, 1);
    const auto found = std::find_if(settings.devices.begin(), settings.devices.end(),
                                    [&](const WledDevice &device)
                                    {
                                        return device.name == name;
                                    });
    if (found == settings.devices.end())
    {
        reject("arguments");
    }
    return *found;
}

void check_wled_binding(const WledSettings &settings, const WledDevice &device, const Json &arguments, const Json &scope)
{
    if (!device.allow_control)
    {
        reject("scope");
    }
    if (string_field(arguments, "target_digest", 64, true, 64) != wled_binding(settings, device, scope))
    {
        reject("binding");
    }
}

bool colors_valid(const Json &colors)
{
    return colors.is_array() && !colors.empty() && colors.size() <= 3 &&
           std::all_of(colors.begin(), colors.end(),
                       [](const Json &color)
                       {
                           return color.is_array() && color.size() >= 3 && color.size() <= 4 &&
                                  std::all_of(color.begin(), color.end(),
                                              [](const Json &channel)
                                              {
                                                  return integer_in(channel, 0, 255);
                                              });
                       });
}

void wled_provider(const Json &data)
{
    if (!data.is_object())
    {
        reject("protocol");
    }
    if ((data.contains("success") && data.at("success") == false) ||
        (data.contains("error") && !data.at("error").is_null() && data.at("error") != false && data.at("error") != 0 && data.at("error") != ""))
    {
        reject("provider");
    }
}

Json read_wled(Context &context, const WledSettings &settings, const WledDevice &device)
{
    const auto data = decode(send(context, request_for(device.base_url + "/json/si", "GET", nullptr, settings.timeout, settings.maximum, device.allow_http)));
    wled_provider(data);
    if (!data.contains("state") || !data.at("state").is_object() || !data.contains("info") || !data.at("info").is_object())
    {
        reject("protocol");
    }
    const auto &state = data.at("state");
    const auto &info = data.at("info");
    if (!state.contains("on") || !state.at("on").is_boolean() || !state.contains("bri") || !integer_in(state.at("bri"), 1, 255) || !state.contains("seg") ||
        !state.at("seg").is_array() || state.at("seg").size() > 64 || !info.contains("leds") || !info.at("leds").is_object())
    {
        reject("protocol");
    }
    const auto &leds = info.at("leds");
    Json segments = Json::array();
    std::set<std::int64_t> seen;
    for (const auto &raw : state.at("seg"))
    {
        if (!raw.is_object() || !raw.contains("id") || !integer_in(raw.at("id"), 0, 63))
        {
            reject("protocol");
        }
        const auto id = raw.at("id").get<std::int64_t>();
        if (!seen.insert(id).second)
        {
            reject("protocol");
        }
        const auto start = optional_integer(raw, "start", 0, 65535);
        const auto stop = optional_integer(raw, "stop", 0, 65535);
        Json capability;
        if (leds.contains("seglc") && leds.at("seglc").is_array() && leds.at("seglc").size() <= 64 && static_cast<std::size_t>(id) < leds.at("seglc").size() &&
            integer_in(leds.at("seglc").at(static_cast<std::size_t>(id)), 0, 255))
        {
            capability = leds.at("seglc").at(static_cast<std::size_t>(id));
        }
        segments.push_back({{"id", id},
                            {"bounds_reported", !start.is_null() && !stop.is_null()},
                            {"active", !start.is_null() && !stop.is_null() && stop > start},
                            {"on", optional_boolean(raw, "on")},
                            {"bri", optional_integer(raw, "bri", 0, 255)},
                            {"fx", optional_integer(raw, "fx", 0, 65535)},
                            {"lc", capability},
                            {"col", raw.contains("col") && colors_valid(raw.at("col")) ? raw.at("col") : Json()}});
    }
    Json firmware;
    if (info.contains("ver") && info.at("ver").is_string())
    {
        const auto value = info.at("ver").get<std::string>();
        if (value.size() <= 64 && printable(value))
        {
            firmware = value;
        }
    }
    Json nightlight;
    if (state.contains("nl") && state.at("nl").is_object())
    {
        nightlight = optional_boolean(state.at("nl"), "on");
    }
    return {{"on", state.at("on")},
            {"bri", state.at("bri")},
            {"transition", optional_integer(state, "transition", 0, 65535)},
            {"pl", optional_integer(state, "pl", -1, 250)},
            {"nl", nightlight},
            {"live", optional_boolean(info, "live")},
            {"firmware", firmware},
            {"maxseg", optional_integer(leds, "maxseg", 1, 255)},
            {"led_count", optional_integer(leds, "count", 1, 100000)},
            {"seg", segments}};
}

Json public_wled(Context &context, const WledDevice &device, const Json &snapshot)
{
    Json segments = Json::array();
    std::set<std::int64_t> found;
    for (const auto &segment : snapshot.at("seg"))
    {
        const auto id = segment.at("id").get<std::int64_t>();
        if (std::find(device.segments.begin(), device.segments.end(), id) == device.segments.end())
        {
            continue;
        }
        found.insert(id);
        const auto &capability = segment.at("lc");
        const bool reported = !capability.is_null();
        const auto flags = reported ? capability.get<std::int64_t>() : 0;
        segments.push_back(
            {{"id", id},
             {"active", segment.at("active")},
             {"on", segment.at("on")},
             {"brightness", segment.at("bri")},
             {"colors", segment.at("col")},
             {"effect", segment.at("fx")},
             {"capabilities",
              {{"reported", reported}, {"rgb", reported ? Json((flags & 1) != 0) : Json()}, {"white", reported ? Json((flags & 2) != 0) : Json()}}}});
    }
    Json missing = Json::array();
    for (const auto id : device.segments)
    {
        if (!found.contains(id))
        {
            missing.push_back(id);
        }
    }
    return {{"device", device.name},
            {"observed_at", context.timestamp()},
            {"cached", false},
            {"on", snapshot.at("on")},
            {"brightness", snapshot.at("bri")},
            {"firmware", snapshot.at("firmware")},
            {"transition_deciseconds", snapshot.at("transition")},
            {"playlist_id", snapshot.at("pl")},
            {"nightlight_active", snapshot.at("nl")},
            {"realtime_active", snapshot.at("live")},
            {"led_count", snapshot.at("led_count")},
            {"max_segments", snapshot.at("maxseg")},
            {"segments", segments},
            {"missing_segments", missing},
            {"physical_output_verified", false}};
}

bool wled_modes_safe(const Json &snapshot)
{
    return snapshot.at("live") == false && snapshot.at("pl") == -1 && snapshot.at("nl") == false;
}

const Json *find_segment(const Json &snapshot, const std::int64_t id)
{
    for (const auto &segment : snapshot.at("seg"))
    {
        if (segment.at("id") == id)
        {
            return &segment;
        }
    }
    return nullptr;
}

void validate_wled_patch(const WledDevice &device, const Json &before, const Json &patch)
{
    if (!device.allow_control || !wled_modes_safe(before))
    {
        reject("safety");
    }
    const bool global = patch.contains("on") || patch.contains("bri") || patch.contains("transition");
    if (global && !device.allow_global)
    {
        reject("scope");
    }
    if (patch.contains("transition") && (before.at("transition").is_null() || !integer_in(patch.at("transition"), 0, device.max_transition)))
    {
        reject("safety");
    }
    if (patch.contains("bri") && !integer_in(patch.at("bri"), 1, device.max_brightness))
    {
        reject("safety");
    }
    if (patch.contains("on") && patch.at("on") == true)
    {
        if (patch.value("bri", before.at("bri")) > device.max_brightness || before.at("seg").empty() || before.at("maxseg").is_null() ||
            before.at("led_count").is_null())
        {
            reject("safety");
        }
        for (const auto &segment : before.at("seg"))
        {
            if (segment.at("bounds_reported") != true || segment.at("id") >= before.at("maxseg") ||
                (segment.at("active") == true && !integer_in(segment.at("bri"), 0, device.max_brightness)))
            {
                reject("safety");
            }
        }
    }
    std::set<std::int64_t> seen;
    for (const auto &requested : patch.value("seg", Json::array()))
    {
        const auto id = requested.at("id").get<std::int64_t>();
        if (std::find(device.segments.begin(), device.segments.end(), id) == device.segments.end() || !seen.insert(id).second)
        {
            reject("scope");
        }
        const auto *actual = find_segment(before, id);
        if (actual == nullptr || actual->at("active") != true || before.at("maxseg").is_null() || before.at("maxseg") <= id)
        {
            reject("safety");
        }
        for (const auto *key : {"on", "bri", "fx"})
        {
            if (requested.contains(key) && actual->at(key).is_null())
            {
                reject("safety");
            }
        }
        if (requested.contains("bri") && !integer_in(requested.at("bri"), 1, device.max_brightness))
        {
            reject("safety");
        }
        if (requested.contains("col"))
        {
            if (actual->at("lc").is_null() || actual->at("col").is_null() || (actual->at("lc").get<std::int64_t>() & 1) == 0 || !requested.contains("fx") ||
                requested.at("fx") != 0)
            {
                reject("safety");
            }
            for (const auto &color : requested.at("col"))
            {
                if (color.size() == 4 && (actual->at("lc").get<std::int64_t>() & 2) == 0)
                {
                    reject("safety");
                }
            }
        }
        const bool can_light =
            requested.contains("col") || requested.contains("bri") || requested.contains("fx") || (requested.contains("on") && requested.at("on") == true);
        if (can_light &&
            (patch.value("bri", before.at("bri")) > device.max_brightness || !integer_in(requested.value("bri", actual->at("bri")), 0, device.max_brightness)))
        {
            reject("safety");
        }
    }
}

Json static_preset(Context &context, const WledSettings &settings, const WledDevice &device, const WledPreset &preset)
{
    const auto data =
        decode(send(context, request_for(device.base_url + "/presets.json", "GET", nullptr, settings.timeout, settings.maximum, device.allow_http)));
    if (!data.is_object() || !data.contains(std::to_string(preset.id)) || !data.at(std::to_string(preset.id)).is_object())
    {
        reject("protocol");
    }
    const auto &raw = data.at(std::to_string(preset.id));
    try
    {
        check_object(raw, {"n", "ql", "on", "bri", "transition", "seg"});
        for (const auto &[key, maximum] : std::map<std::string, std::size_t>{{"n", 128}, {"ql", 16}})
        {
            if (raw.contains(key) && !printable(string_field(raw, key, maximum, true)))
            {
                reject("safety");
            }
        }
        if (raw.contains("on") && !raw.at("on").is_boolean())
        {
            reject("safety");
        }
        if ((raw.contains("bri") && !integer_in(raw.at("bri"), 1, device.max_brightness)) ||
            (raw.contains("transition") && !integer_in(raw.at("transition"), 0, device.max_transition)))
        {
            reject("safety");
        }
        if (raw.contains("seg"))
        {
            const auto &segments = raw.at("seg");
            if (!segments.is_array() || segments.empty() || segments.size() > 16)
            {
                reject("safety");
            }
            for (const auto &segment : segments)
            {
                check_object(segment, {"id", "on", "bri", "col", "fx"}, {"id"});
                if (!integer_in(segment.at("id"), 0, 63) || segment.size() < 2 || (segment.contains("on") && !segment.at("on").is_boolean()) ||
                    (segment.contains("bri") && !integer_in(segment.at("bri"), 1, device.max_brightness)) ||
                    (segment.contains("fx") && !integer_in(segment.at("fx"), 0, 0)) ||
                    (segment.contains("col") && (!colors_valid(segment.at("col")) || !segment.contains("fx") || segment.at("fx") != 0)))
                {
                    reject("safety");
                }
            }
        }
    }
    catch (const std::exception &)
    {
        reject("safety");
    }
    if (hash(raw) != preset.sha256)
    {
        reject("binding");
    }
    auto patch = raw;
    patch.erase("n");
    patch.erase("ql");
    if (patch.empty())
    {
        reject("safety");
    }
    return patch;
}

bool wled_matches(const Json &after, const Json &patch)
{
    if (!wled_modes_safe(after))
    {
        return false;
    }
    for (const auto *key : {"on", "bri", "transition"})
    {
        if (patch.contains(key) && patch.at(key) != after.at(key))
        {
            return false;
        }
    }
    for (const auto &requested : patch.value("seg", Json::array()))
    {
        const auto *actual = find_segment(after, requested.at("id").get<std::int64_t>());
        if (actual == nullptr || actual->at("active") != true)
        {
            return false;
        }
        for (const auto *key : {"on", "bri", "fx"})
        {
            if (requested.contains(key) && requested.at(key) != actual->at(key))
            {
                return false;
            }
        }
        if (requested.contains("col"))
        {
            if (actual->at("col").is_null() || actual->at("col").size() < requested.at("col").size())
            {
                return false;
            }
            for (std::size_t index = 0; index < requested.at("col").size(); ++index)
            {
                auto expected = requested.at("col").at(index);
                auto observed = actual->at("col").at(index);
                if (expected.size() == 3)
                {
                    expected.push_back(0);
                }
                if (observed.size() == 3)
                {
                    observed.push_back(0);
                }
                if (expected != observed)
                {
                    return false;
                }
            }
        }
    }
    return true;
}

Json perform_wled(Context &context, const WledSettings &settings, const WledDevice &device, const std::string &operation, const Json &arguments,
                  const Json &patch)
{
    const auto before = read_wled(context, settings, device);
    validate_wled_patch(device, before, patch);
    Json result = {{"device", device.name},        {"operation", operation}, {"target_digest", arguments.at("target_digest")},
                   {"requested", patch},           {"attempted", true},      {"acknowledged", nullptr},
                   {"command_error", nullptr},     {"observation", nullptr}, {"readback_error", nullptr},
                   {"matches_requested", nullptr}, {"status", "uncertain"},  {"physical_output_verified", false},
                   {"automatic_resend", false}};
    auto payload = patch;
    payload["udpn"] = {{"nn", true}};
    try
    {
        const auto acknowledgement =
            decode(send(context, request_for(device.base_url + "/json/state", "POST", &payload, settings.timeout, settings.maximum, device.allow_http)));
        wled_provider(acknowledgement);
        if (!acknowledgement.contains("success") || !acknowledgement.at("success").is_boolean())
        {
            reject("protocol");
        }
        result["acknowledged"] = acknowledgement.at("success");
    }
    catch (const Error &error)
    {
        result["command_error"] = error.code();
        if (error.code() == "devices_provider")
        {
            result["acknowledged"] = false;
        }
    }
    try
    {
        const auto after = read_wled(context, settings, device);
        result["observation"] = public_wled(context, device, after);
        result["matches_requested"] = wled_matches(after, patch);
    }
    catch (const Error &error)
    {
        result["readback_error"] = error.code();
    }
    if (result.at("acknowledged") == true)
    {
        result["status"] = result.at("matches_requested").is_null() ? "unconfirmed"
                           : result.at("matches_requested") == true ? "state_matched"
                                                                    : "state_mismatch";
    }
    else if (result.at("acknowledged") == false)
    {
        result["status"] = "rejected";
    }
    mark_outcome(result);
    return result;
}

Json wled_inventory(const WledSettings &settings)
{
    Json devices = Json::array();
    for (const auto &device : settings.devices)
    {
        Json targets = Json::array();
        Json presets = Json::array();
        if (device.allow_control)
        {
            for (const auto segment : device.segments)
            {
                targets.push_back({{"scope", "segment"}, {"segment", segment}, {"target_digest", wled_binding(settings, device, segment_scope(segment))}});
            }
            if (device.allow_global)
            {
                targets.push_back({{"scope", "global"}, {"target_digest", wled_binding(settings, device, global_scope())}});
            }
        }
        for (const auto &preset : device.presets)
        {
            Json item = {{"name", preset.name}, {"id", preset.id}, {"sha256", preset.sha256}};
            if (device.allow_control)
            {
                item["target_digest"] = wled_binding(settings, device, preset_scope(preset));
            }
            presets.push_back(std::move(item));
        }
        devices.push_back({{"device", device.name},
                           {"control_enabled", device.allow_control},
                           {"global_control", device.allow_global},
                           {"segments", device.segments},
                           {"max_brightness", device.max_brightness},
                           {"max_transition_deciseconds", device.max_transition},
                           {"targets", targets},
                           {"presets", presets}});
    }
    return {{"configured_only", true}, {"live_validation", false}, {"polling", false}, {"devices", devices}};
}

void register_wled(Context &context, Registry &registry)
{
    const auto settings = std::make_shared<WledSettings>(wled_settings(context.settings("wled")));
    Json names = Json::array();
    Json controlled = Json::array();
    Json targets = Json::array();
    Json globals = Json::array();
    Json preset_targets = Json::array();
    Json preset_names = Json::array();
    for (const auto &device : settings->devices)
    {
        names.push_back(device.name);
        if (!device.allow_control)
        {
            continue;
        }
        controlled.push_back(device.name);
        for (const auto segment : device.segments)
        {
            targets.push_back(wled_binding(*settings, device, segment_scope(segment)));
        }
        if (device.allow_global)
        {
            const auto binding = wled_binding(*settings, device, global_scope());
            targets.push_back(binding);
            globals.push_back(binding);
        }
        for (const auto &preset : device.presets)
        {
            preset_targets.push_back(wled_binding(*settings, device, preset_scope(preset)));
            preset_names.push_back(preset.name);
        }
    }
    auto device_schema = text_schema(32);
    device_schema["enum"] = names;
    add_tool(registry, "wled_devices", "Offline configured WLED aliases, safety limits and scope-bound action fingerprints.", Tier::read,
             object_schema(Json::object()),
             [settings](const Json &)
             {
                 return wled_inventory(*settings);
             });
    add_tool(registry, "wled_status", "Read one named WLED state/capability snapshot; not physical output confirmation.", Tier::read,
             object_schema({{"device", device_schema}}, {"device"}),
             [&context, settings](const Json &arguments)
             {
                 const auto &device = wled_device(*settings, arguments);
                 return public_wled(context, device, read_wled(context, *settings, device));
             });
    if (controlled.empty())
    {
        return;
    }
    device_schema["enum"] = controlled;
    const auto action_schema = [&](const Json &properties, const std::vector<std::string> &required, const Json &bindings)
    {
        Json fields = {{"device", device_schema}, {"target_digest", binding_schema(bindings)}};
        fields.update(properties);
        std::vector<std::string> keys{"device", "target_digest"};
        keys.insert(keys.end(), required.begin(), required.end());
        return object_schema(fields, keys);
    };
    const auto summary = [](const Json &arguments)
    {
        auto values = arguments;
        values.erase("device");
        values.erase("target_digest");
        values.erase("segment");
        return "WLED " + arguments.at("device").get<std::string>() +
               (arguments.contains("segment") ? " segment " + arguments.at("segment").dump() : " reviewed global/preset scope") + " " +
               values.dump(-1, ' ', true) + "; one write attempt and one readback";
    };
    for (const std::string operation : {"set_power", "set_brightness", "set_color"})
    {
        Json properties = {{"segment", integer_schema(0, 63)}};
        std::vector<std::string> required;
        if (operation == "set_power")
        {
            properties["on"] = {{"type", "boolean"}};
            required = {"on"};
        }
        else if (operation == "set_brightness")
        {
            properties["brightness"] = integer_schema(1, 255);
            required = {"brightness"};
        }
        else
        {
            properties["color"] = {{"type", "array"}, {"minItems", 3}, {"maxItems", 4}, {"items", integer_schema(0, 255)}};
            required = {"segment", "color"};
        }
        add_tool(
            registry, "wled_" + operation, "Approval-gated absolute WLED control within a current inventory binding.", Tier::act,
            action_schema(properties, required, targets),
            [&context, settings, operation](const Json &arguments)
            {
                const auto &device = wled_device(*settings, arguments);
                const auto scope = arguments.contains("segment") ? segment_scope(required_integer(arguments, "segment", 0, 63)) : global_scope();
                check_wled_binding(*settings, device, arguments, scope);
                Json patch;
                if (operation == "set_power")
                {
                    patch = {{"on", arguments.at("on")}};
                }
                else if (operation == "set_brightness")
                {
                    patch = {{"bri", arguments.at("brightness")}};
                }
                else
                {
                    patch = {{"col", Json::array({arguments.at("color")})}, {"fx", 0}};
                }
                if (arguments.contains("segment"))
                {
                    patch["id"] = arguments.at("segment");
                    patch = {{"seg", Json::array({patch})}};
                }
                return perform_wled(context, *settings, device, operation, arguments, patch);
            },
            summary);
    }
    if (!globals.empty())
    {
        add_tool(
            registry, "wled_set_transition", "Set the permitted global persistent transition, at most 100 deciseconds.", Tier::act,
            action_schema({{"transition_deciseconds", integer_schema(0, MAX_TRANSITION)}}, {"transition_deciseconds"}, globals),
            [&context, settings](const Json &arguments)
            {
                const auto &device = wled_device(*settings, arguments);
                check_wled_binding(*settings, device, arguments, global_scope());
                return perform_wled(context, *settings, device, "set_transition", arguments, {{"transition", arguments.at("transition_deciseconds")}});
            },
            summary);
    }
    if (!preset_targets.empty())
    {
        auto preset_schema = text_schema(32);
        preset_schema["enum"] = preset_names;
        add_tool(
            registry, "wled_apply_preset", "Copy only a fingerprint-reviewed static preset; never execute a firmware preset macro.", Tier::act,
            action_schema({{"preset", preset_schema}}, {"preset"}, preset_targets),
            [&context, settings](const Json &arguments)
            {
                const auto &device = wled_device(*settings, arguments);
                const auto selected = std::find_if(device.presets.begin(), device.presets.end(),
                                                   [&](const WledPreset &preset)
                                                   {
                                                       return preset.name == arguments.at("preset");
                                                   });
                if (selected == device.presets.end())
                {
                    reject("scope");
                }
                check_wled_binding(*settings, device, arguments, preset_scope(*selected));
                const auto patch = static_preset(context, *settings, device, *selected);
                return perform_wled(context, *settings, device, "apply_preset", arguments, patch);
            },
            summary);
    }
}

struct ShellyDevice
{
    std::string name;
    std::string base_url;
    std::string expected_id;
    std::string model;
    std::optional<std::string> password;
    bool allow_http;
    bool allow_control;
    std::int64_t watts;
};

struct ShellySettings
{
    std::int64_t timeout;
    std::vector<ShellyDevice> devices;
};

bool secret_reference(const std::string &reference)
{
    if (reference.size() < 5 || reference.size() > 512 || !printable(reference))
    {
        return false;
    }
    if (reference.starts_with("env:"))
    {
        const auto variable = reference.substr(4);
        const auto letter = [](const unsigned char character)
        {
            return (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') || character == '_';
        };
        return !variable.empty() && letter(variable.front()) &&
               std::all_of(variable.begin(), variable.end(),
                           [&](const unsigned char character)
                           {
                               return letter(character) || (character >= '0' && character <= '9');
                           });
    }
    return reference.starts_with("file:/") || reference.starts_with("file:~/");
}

ShellySettings shelly_settings(const Json &settings)
{
    try
    {
        check_object(settings, {"devices", "timeout_seconds"}, {"devices"});
        ShellySettings result{integer_field(settings, "timeout_seconds", 1, 15, 5), {}};
        const auto &devices = settings.at("devices");
        if (!devices.is_object() || devices.empty() || devices.size() > MAX_DEVICES)
        {
            reject("config");
        }
        std::set<std::string> endpoints;
        std::set<std::string> identities;
        for (const auto &[name, raw] : devices.items())
        {
            check_object(raw,
                         {"base_url", "allow_http", "expected_device_id", "expected_model", "expected_generation", "switch_ids", "password", "allow_control",
                          "controls"},
                         {"base_url", "expected_device_id", "expected_model", "expected_generation", "switch_ids"});
            ShellyDevice device;
            device.name = name;
            device.allow_http = boolean_field(raw, "allow_http");
            device.allow_control = boolean_field(raw, "allow_control");
            device.base_url = origin(string_field(raw, "base_url", 300, true, 1), device.allow_http, false);
            device.expected_id = string_field(raw, "expected_device_id", 96, true, 1);
            device.model = string_field(raw, "expected_model", 48, true, 1);
            device.watts = 0;
            const auto first = device.expected_id.front();
            if (!name_valid(name) || !((first >= 'a' && first <= 'z') || (first >= '0' && first <= '9')) || !endpoints.insert(device.base_url).second ||
                !identities.insert(device.expected_id).second ||
                !std::all_of(device.expected_id.begin(), device.expected_id.end(),
                             [](const unsigned char character)
                             {
                                 return (character >= 'a' && character <= 'z') || (character >= '0' && character <= '9') || character == '_' ||
                                        character == '-';
                             }) ||
                (device.model != "SNPL-00112EU" && device.model != "SNPL-00116US") || required_integer(raw, "expected_generation", 2, 2) != 2 ||
                !raw.at("switch_ids").is_array() || raw.at("switch_ids").size() != 1 || !integer_in(raw.at("switch_ids").at(0), 0, 0))
            {
                reject("config");
            }
            if (raw.contains("password"))
            {
                const auto reference = string_field(raw, "password", 512, true, 1);
                if (!secret_reference(reference))
                {
                    reject("config");
                }
                device.password = reference;
            }
            const auto controls = raw.value("controls", Json::object());
            if (!controls.is_object() || device.allow_control != !controls.empty())
            {
                reject("config");
            }
            if (device.allow_control)
            {
                if (!device.password || controls.size() != 1 || !controls.contains("0"))
                {
                    reject("config");
                }
                const auto &control = controls.at("0");
                check_object(control, {"safe_load", "max_power_watts", "safety_acknowledged"}, {"safe_load", "max_power_watts", "safety_acknowledged"});
                if (string_field(control, "safe_load", 32, true, 1) != "nonessential_lighting" || !boolean_field(control, "safety_acknowledged"))
                {
                    reject("config");
                }
                device.watts = required_integer(control, "max_power_watts", 1, 100);
            }
            result.devices.push_back(std::move(device));
        }
        return result;
    }
    catch (const std::exception &)
    {
        reject("config");
    }
}

std::string shelly_binding(const ShellySettings &settings, const ShellyDevice &device)
{
    return hash({{"engine", "switchboard_cpp_shelly_plug_v1"},
                 {"device", device.name},
                 {"base_url", device.base_url},
                 {"device_id", device.expected_id},
                 {"model", device.model},
                 {"generation", 2},
                 {"switch_id", 0},
                 {"switch_ids", Json::array({0})},
                 {"allow_http", device.allow_http},
                 {"allow_control", device.allow_control},
                 {"password_reference", device.password ? Json(*device.password) : Json()},
                 {"safe_load", "nonessential_lighting"},
                 {"safety_acknowledged", true},
                 {"max_power_watts", device.watts},
                 {"absolute_max_power_watts", 100},
                 {"timeout_seconds", settings.timeout},
                 {"max_response_bytes", SHELLY_BYTES},
                 {"reject_active_timers", true},
                 {"digest_algorithm", "SHA-256"},
                 {"digest_qop", "auth"}});
}

const ShellyDevice &shelly_device(const ShellySettings &settings, const Json &arguments)
{
    const auto name = string_field(arguments, "device", 32, true, 1);
    const auto selected = std::find_if(settings.devices.begin(), settings.devices.end(),
                                       [&](const ShellyDevice &device)
                                       {
                                           return device.name == name;
                                       });
    if (selected == settings.devices.end() || required_integer(arguments, "switch_id", 0, 0) != 0)
    {
        reject("arguments");
    }
    return *selected;
}

void validate_challenge(const HttpResponse &response, const std::string &expected_realm)
{
    std::string header;
    bool found = false;
    for (const auto &[key, value] : response.headers)
    {
        if (lowercase(key) == "www-authenticate")
        {
            if (found)
            {
                reject("auth");
            }
            header = value;
            found = true;
        }
    }
    if (!found)
    {
        reject("auth");
    }
    validate_digest_challenge(header, expected_realm);
}

class ShellySession
{
public:
    ShellySession(Context &context, const ShellySettings &settings, const ShellyDevice &device) : context_(context), settings_(settings), device_(device)
    {
    }

    void identify()
    {
        const auto result = rpc("Shelly.GetDeviceInfo", {{"ident", false}}, false);
        if (!result.contains("id") || result.at("id") != device_.expected_id || !result.contains("model") || result.at("model") != device_.model ||
            !result.contains("gen") || !integer_in(result.at("gen"), 2, 2))
        {
            reject("identity");
        }
        if (!result.contains("auth_en") || !result.at("auth_en").is_boolean() || result.at("auth_en").get<bool>() != device_.password.has_value())
        {
            reject("auth");
        }
        if (result.contains("profile") && result.at("profile") != "switch")
        {
            reject("identity");
        }
        identified_ = true;
    }

    Json status()
    {
        if (!identified_)
        {
            reject("identity");
        }
        return rpc("Switch.GetStatus", {{"id", 0}}, false);
    }

    Json set(const bool on)
    {
        if (!identified_ || !device_.allow_control || !password_)
        {
            reject("scope");
        }
        return rpc("Switch.Set", {{"id", 0}, {"on", on}}, true);
    }

private:
    void resolve_password()
    {
        if (password_)
        {
            return;
        }
        try
        {
            const auto password = context_.secret(*device_.password);
            if (password.empty() || password.size() > 1024 || password.find_first_of("\r\n") != std::string::npos || password.find('\0') != std::string::npos)
            {
                reject("secret");
            }
            password_ = password;
        }
        catch (const std::exception &)
        {
            reject("secret");
        }
    }

    Json rpc(const std::string &method, const Json &params, const bool action)
    {
        const auto id = ++next_id_;
        const Json payload = {{"id", id}, {"src", "switchboard-native"}, {"method", method}, {"params", params}};
        auto request = request_for(device_.base_url + "/rpc", "POST", &payload, settings_.timeout, SHELLY_BYTES, device_.allow_http);
        if (action)
        {
            request.digest_password = password_;
            request.digest_realm = device_.expected_id;
        }
        auto response = send(context_, request);
        const bool public_method = method == "Shelly.GetDeviceInfo";
        if (response.status == 401)
        {
            if (action || public_method || !device_.password)
            {
                reject("auth");
            }
            validate_challenge(response, device_.expected_id);
            resolve_password();
            request.digest_password = password_;
            request.digest_realm = device_.expected_id;
            response = send(context_, request);
            if (response.status == 401 || response.status == 403)
            {
                reject("auth");
            }
        }
        else if (response.status == 403)
        {
            reject("auth");
        }
        else if (!public_method && !action && device_.password && response.status >= 200 && response.status < 300)
        {
            reject("auth");
        }
        const auto frame = decode(response);
        if (!frame.is_object() || !frame.contains("id") || !integer_in(frame.at("id"), id, id) || !frame.contains("src") ||
            frame.at("src") != device_.expected_id || (frame.contains("dst") && frame.at("dst") != "switchboard-native") ||
            frame.contains("result") == frame.contains("error"))
        {
            reject("protocol");
        }
        if (frame.contains("error"))
        {
            if (!frame.at("error").is_object() || !frame.at("error").contains("code") ||
                !integer_in(frame.at("error").at("code"), -MAX_JSON_INTEGER, MAX_JSON_INTEGER))
            {
                reject("protocol");
            }
            reject("provider");
        }
        if (!frame.at("result").is_object())
        {
            reject("protocol");
        }
        return frame.at("result");
    }

    Context &context_;
    const ShellySettings &settings_;
    const ShellyDevice &device_;
    std::optional<std::string> password_;
    std::int64_t next_id_ = 0;
    bool identified_ = false;
};

Json measurement(const Json &value, const std::string &unit, const double minimum, const double maximum)
{
    if (!value.is_null() && (!value.is_number() || !std::isfinite(value.get<double>()) || value.get<double>() < minimum || value.get<double>() > maximum))
    {
        reject("protocol");
    }
    return {{"available", !value.is_null()}, {"value", value}, {"unit", unit}};
}

Json energy(const Json &raw, const std::string &key)
{
    const auto container = raw.value(key, Json());
    if (!container.is_null() && !container.is_object())
    {
        reject("protocol");
    }
    auto result = measurement(container.is_object() ? container.value("total", Json()) : Json(), "Wh", 0, 1.0e12);
    result["kind"] = "device_cumulative_counter";
    return result;
}

Json shelly_snapshot(Context &context, ShellySession &session, const ShellyDevice &device)
{
    const auto raw = session.status();
    if (!raw.contains("id") || !integer_in(raw.at("id"), 0, 0) || !raw.contains("output") || !raw.at("output").is_boolean())
    {
        reject("protocol");
    }
    const auto errors = raw.value("errors", Json::array());
    if (!errors.is_array() || errors.size() > 16)
    {
        reject("protocol");
    }
    static const std::set<std::string> known{"overtemp", "overpower", "overvoltage", "undervoltage", "overcurrent"};
    std::set<std::string> safe_errors;
    for (const auto &error : errors)
    {
        if (!error.is_string() || error.get_ref<const std::string &>().empty() || error.get_ref<const std::string &>().size() > 64)
        {
            reject("protocol");
        }
        const auto value = error.get<std::string>();
        safe_errors.insert(known.contains(value) ? value : "unrecognized_error");
    }
    const auto started = raw.value("timer_started_at", Json());
    const auto duration = raw.value("timer_duration", Json());
    const auto timer_type = raw.value("timer_type", Json());
    if (started.is_null() != duration.is_null())
    {
        reject("protocol");
    }
    static const std::set<std::string> timer_types{"auto_on", "auto_off", "power_restore_delay", "toggle_after"};
    if (!timer_type.is_null() && (started.is_null() || !timer_type.is_string() || !timer_types.contains(timer_type.get<std::string>())))
    {
        reject("protocol");
    }
    if (!started.is_null())
    {
        (void)measurement(started, "s", 0, static_cast<double>(MAX_JSON_INTEGER));
        (void)measurement(duration, "s", 0, 1.0e9);
    }
    return {{"device", device.name},
            {"switch_id", 0},
            {"observed_at", context.timestamp()},
            {"cached", false},
            {"measurement_age_seconds", nullptr},
            {"output", raw.at("output")},
            {"power", measurement(raw.value("apower", Json()), "W", -1.0e6, 1.0e6)},
            {"energy", energy(raw, "aenergy")},
            {"returned_energy", energy(raw, "ret_aenergy")},
            {"voltage", measurement(raw.value("voltage", Json()), "V", 0, 1000)},
            {"current", measurement(raw.value("current", Json()), "A", -10000, 10000)},
            {"errors", safe_errors},
            {"timer_active", !started.is_null()},
            {"physical_output_verified", false}};
}

bool shelly_safe(const Json &snapshot, const ShellyDevice &device)
{
    const auto &power = snapshot.at("power").at("value");
    return !power.is_null() && power >= 0 && power <= device.watts && snapshot.at("errors").empty() && snapshot.at("timer_active") == false;
}

Json perform_shelly(Context &context, const ShellySettings &settings, const ShellyDevice &device, const Json &arguments)
{
    if (!device.allow_control)
    {
        reject("scope");
    }
    if (string_field(arguments, "target", 64, true, 64) != shelly_binding(settings, device))
    {
        reject("binding");
    }
    ShellySession session(context, settings, device);
    session.identify();
    const auto before = shelly_snapshot(context, session, device);
    if (!shelly_safe(before, device))
    {
        reject("safety");
    }
    const auto on = arguments.at("on").get<bool>();
    Json result = {{"device", device.name},
                   {"switch_id", 0},
                   {"target", arguments.at("target")},
                   {"requested_output", on},
                   {"attempted", true},
                   {"command_accepted", nullptr},
                   {"was_on", nullptr},
                   {"command_error", nullptr},
                   {"observation", nullptr},
                   {"readback_error", nullptr},
                   {"matches_requested", nullptr},
                   {"readback_safe", nullptr},
                   {"status", "uncertain"},
                   {"physical_output_verified", false},
                   {"automatic_resend", false}};
    try
    {
        const auto acknowledgement = session.set(on);
        if (!acknowledgement.contains("was_on") || !acknowledgement.at("was_on").is_boolean())
        {
            reject("protocol");
        }
        result["command_accepted"] = true;
        result["was_on"] = acknowledgement.at("was_on");
    }
    catch (const Error &error)
    {
        result["command_error"] = error.code();
        if (error.code() == "devices_auth")
        {
            result["command_accepted"] = false;
        }
    }
    try
    {
        const auto observation = shelly_snapshot(context, session, device);
        result["observation"] = observation;
        result["matches_requested"] = observation.at("output") == on;
        result["readback_safe"] = shelly_safe(observation, device);
    }
    catch (const Error &error)
    {
        result["readback_error"] = error.code();
    }
    if (result.at("command_accepted") == true)
    {
        result["status"] = result.at("matches_requested").is_null() ? "unconfirmed"
                           : result.at("readback_safe") != true     ? "unsafe_readback"
                           : result.at("matches_requested") == true ? "state_matched"
                                                                    : "state_mismatch";
    }
    else if (result.at("command_accepted") == false)
    {
        result["status"] = "rejected";
    }
    mark_outcome(result);
    return result;
}

void register_shelly(Context &context, Registry &registry)
{
    const auto settings = std::make_shared<ShellySettings>(shelly_settings(context.settings("shelly")));
    Json names = Json::array();
    Json controlled = Json::array();
    Json targets = Json::array();
    for (const auto &device : settings->devices)
    {
        names.push_back(device.name);
        if (device.allow_control)
        {
            controlled.push_back(device.name);
            targets.push_back(shelly_binding(*settings, device));
        }
    }
    auto device_schema = text_schema(32);
    device_schema["enum"] = names;
    add_tool(registry, "shelly_devices", "Offline configured plug models and reviewed component-bound action fingerprints.", Tier::read,
             object_schema(Json::object()),
             [settings](const Json &)
             {
                 Json devices = Json::array();
                 for (const auto &device : settings->devices)
                 {
                     Json controls = Json::array();
                     if (device.allow_control)
                     {
                         controls.push_back({{"switch_id", 0},
                                             {"safe_load", "nonessential_lighting"},
                                             {"max_power_watts", device.watts},
                                             {"target", shelly_binding(*settings, device)}});
                     }
                     devices.push_back({{"device", device.name},
                                        {"model", device.model},
                                        {"generation", 2},
                                        {"switch_ids", Json::array({0})},
                                        {"authentication_configured", device.password.has_value()},
                                        {"controls", controls}});
                 }
                 return Json{{"configured_only", true}, {"live_validation", false}, {"polling", false}, {"devices", devices}};
             });
    add_tool(registry, "shelly_status", "Read fixed plug output, optional power and cumulative energy; no history or physical confirmation.", Tier::read,
             object_schema({{"device", device_schema}, {"switch_id", integer_schema(0, 0)}}, {"device", "switch_id"}),
             [&context, settings](const Json &arguments)
             {
                 const auto &device = shelly_device(*settings, arguments);
                 ShellySession session(context, *settings, device);
                 session.identify();
                 return shelly_snapshot(context, session, device);
             });
    if (controlled.empty())
    {
        return;
    }
    device_schema["enum"] = controlled;
    add_tool(
        registry, "shelly_switch_set", "Approval-gated absolute output on a reviewed low-power nonessential lighting plug; inspect readback.", Tier::act,
        object_schema({{"device", device_schema}, {"switch_id", integer_schema(0, 0)}, {"target", binding_schema(targets)}, {"on", {{"type", "boolean"}}}},
                      {"device", "switch_id", "target", "on"}),
        [&context, settings](const Json &arguments)
        {
            return perform_shelly(context, *settings, shelly_device(*settings, arguments), arguments);
        },
        [settings](const Json &arguments)
        {
            const auto &device = shelly_device(*settings, arguments);
            return "Shelly " + device.name + " switch:0 " + (arguments.at("on") == true ? "ON" : "OFF") +
                   "; reviewed nonessential lighting <= " + std::to_string(device.watts) + " W; one write attempt and one readback";
        });
}
}  // namespace

void register_devices(Context &context, Registry &registry)
{
    if (context.enabled("wled"))
    {
        register_wled(context, registry);
    }
    if (context.enabled("shelly"))
    {
        register_shelly(context, registry);
    }
}
}  // namespace switchboard

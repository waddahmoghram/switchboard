#include "switchboard/native.hpp"

#include <algorithm>
#include <map>
#include <set>
#include <string>

namespace switchboard
{
namespace
{
[[noreturn]] void reject_digest()
{
    throw Error("devices_auth", "Device authentication is absent, refused or unsupported");
}

bool printable(const std::string &text)
{
    return std::all_of(text.begin(), text.end(),
                       [](const unsigned char character)
                       {
                           return character >= 32 && character <= 126;
                       });
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

bool auth_token(const std::string &value)
{
    return !value.empty() && value.size() <= 256 &&
           std::all_of(value.begin(), value.end(),
                       [](const unsigned char character)
                       {
                           return (character >= 'a' && character <= 'z') || (character >= 'A' && character <= 'Z') || (character >= '0' && character <= '9') ||
                                  character == '+' || character == '/' || character == '_' || character == '=' || character == '.' || character == '-';
                       });
}
}  // namespace

void validate_digest_challenge(const std::string &header, const std::string &expected_realm)
{
    if (header.size() > 2048 || !printable(header) || header.size() < 8 || lowercase(header.substr(0, 7)) != "digest " ||
        header.find('\\') != std::string::npos)
    {
        reject_digest();
    }
    std::map<std::string, std::string> parameters;
    std::size_t position = 7;
    const auto spaces = [&]()
    {
        while (position < header.size() && header[position] == ' ')
        {
            ++position;
        }
    };
    while (position < header.size())
    {
        spaces();
        const auto start = position;
        while (position < header.size() && ((header[position] >= 'a' && header[position] <= 'z') || (header[position] >= 'A' && header[position] <= 'Z') ||
                                            (header[position] >= '0' && header[position] <= '9') || header[position] == '_' || header[position] == '-'))
        {
            ++position;
        }
        const auto key = lowercase(header.substr(start, position - start));
        spaces();
        if (key.empty() || key.size() > 32 || position == header.size() || header[position++] != '=')
        {
            reject_digest();
        }
        spaces();
        std::string value;
        if (position < header.size() && header[position] == '"')
        {
            const auto begin = ++position;
            const auto close = header.find('"', position);
            if (close == std::string::npos)
            {
                reject_digest();
            }
            value = header.substr(begin, close - begin);
            position = close + 1;
        }
        else
        {
            const auto begin = position;
            while (position < header.size() && header[position] != ',' && header[position] != ' ')
            {
                ++position;
            }
            value = header.substr(begin, position - begin);
        }
        if (value.empty() || !parameters.emplace(key, value).second || parameters.size() > 9)
        {
            reject_digest();
        }
        spaces();
        if (position < header.size())
        {
            if (header[position++] != ',')
            {
                reject_digest();
            }
            spaces();
            if (position == header.size())
            {
                reject_digest();
            }
        }
    }
    static const std::set<std::string> allowed{"realm", "nonce", "qop", "algorithm", "opaque", "stale", "charset", "userhash"};
    for (const auto &[key, value] : parameters)
    {
        (void)value;
        if (!allowed.contains(key))
        {
            reject_digest();
        }
    }
    if (parameters["realm"] != expected_realm || lowercase(parameters["algorithm"]) != "sha-256" || parameters["qop"] != "auth" ||
        !auth_token(parameters["nonce"]) || (parameters.contains("opaque") && !auth_token(parameters.at("opaque"))) ||
        (parameters.contains("stale") && parameters.at("stale") != "true" && parameters.at("stale") != "false") ||
        (parameters.contains("charset") && lowercase(parameters.at("charset")) != "utf-8") ||
        (parameters.contains("userhash") && parameters.at("userhash") != "false"))
    {
        reject_digest();
    }
}
}  // namespace switchboard

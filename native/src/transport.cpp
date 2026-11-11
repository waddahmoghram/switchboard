#include "switchboard/native.hpp"

#include <algorithm>
#include <cctype>
#include <limits>
#include <memory>
#include <string>

#include <curl/curl.h>

namespace switchboard
{
namespace
{
struct ResponseBuffer
{
    std::string text;
    std::map<std::string, std::string> headers;
    std::size_t maximum = 0;
    std::size_t header_bytes = 0;
    std::optional<std::string> digest_realm;
    bool digest_challenge_seen = false;
    bool digest_rejected = false;
    bool exceeded = false;
    bool allocation_error = false;
};

std::size_t receive(char *data, const std::size_t size, const std::size_t count, void *opaque) noexcept
{
    auto &buffer = *static_cast<ResponseBuffer *>(opaque);
    if (size != 0 && count > std::numeric_limits<std::size_t>::max() / size)
    {
        buffer.exceeded = true;
        return 0;
    }
    const auto length = size * count;
    if (length > buffer.maximum - buffer.text.size())
    {
        buffer.exceeded = true;
        return 0;
    }
    try
    {
        buffer.text.append(data, length);
    }
    catch (const std::exception &)
    {
        // Exceptions cannot cross libcurl's C callback boundary.
        buffer.allocation_error = true;
        return 0;
    }
    return length;
}

std::size_t receive_header(char *data, const std::size_t size, const std::size_t count, void *opaque) noexcept
{
    auto &buffer = *static_cast<ResponseBuffer *>(opaque);
    if (size != 0 && count > std::numeric_limits<std::size_t>::max() / size)
    {
        buffer.exceeded = true;
        return 0;
    }
    const auto length = size * count;
    if (length > 16384 - buffer.header_bytes)
    {
        buffer.exceeded = true;
        return 0;
    }
    buffer.header_bytes += length;
    try
    {
        const std::string line(data, length);
        if (line.rfind("HTTP/", 0) == 0)
        {
            buffer.text.clear();
            buffer.headers.clear();
        }
        else
        {
            const auto colon = line.find(':');
            if (colon != std::string::npos)
            {
                auto name = line.substr(0, colon);
                std::transform(name.begin(), name.end(), name.begin(),
                               [](const unsigned char character)
                               {
                                   return static_cast<char>(std::tolower(character));
                               });
                if (name == "www-authenticate" || name == "content-type" || name == "retry-after")
                {
                    auto value = line.substr(colon + 1);
                    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.front())) != 0)
                    {
                        value.erase(0, 1);
                    }
                    while (!value.empty() && std::isspace(static_cast<unsigned char>(value.back())) != 0)
                    {
                        value.pop_back();
                    }
                    if (value.size() > 8192 || buffer.headers.contains(name))
                    {
                        buffer.exceeded = true;
                        return 0;
                    }
                    if (name == "www-authenticate" && buffer.digest_realm)
                    {
                        try
                        {
                            validate_digest_challenge(value, *buffer.digest_realm);
                        }
                        catch (const Error &)
                        {
                            buffer.digest_rejected = true;
                            return 0;
                        }
                        buffer.digest_challenge_seen = true;
                    }
                    buffer.headers.emplace(std::move(name), std::move(value));
                }
            }
        }
    }
    catch (const std::exception &)
    {
        buffer.allocation_error = true;
        return 0;
    }
    return length;
}

std::string part(CURLU *url, const CURLUPart component, const bool optional = false)
{
    char *raw = nullptr;
    const auto result = curl_url_get(url, component, &raw, 0);
    if (optional && (result == CURLUE_NO_USER || result == CURLUE_NO_PASSWORD || result == CURLUE_NO_QUERY || result == CURLUE_NO_FRAGMENT))
    {
        return {};
    }
    if (result != CURLUE_OK || raw == nullptr)
    {
        throw Error("invalid", "native endpoint has an invalid address component");
    }
    const std::unique_ptr<char, decltype(&curl_free)> value(raw, curl_free);
    return value.get();
}

void option(const CURLcode result)
{
    if (result != CURLE_OK)
    {
        throw Error("transport", "native web transport options could not be configured");
    }
}

class CurlTransport final : public HttpTransport
{

public:
    CurlTransport()
    {
        static const auto initialized = curl_global_init(CURL_GLOBAL_DEFAULT);
        if (initialized != CURLE_OK)
        {
            throw Error("transport", "native web transport initialization failed");
        }
    }

    HttpResponse request(const HttpRequest &request) override
    {
        validate_endpoint(request.url, request.allow_http);
        if (request.method != "GET" && request.method != "POST" && request.method != "PUT" && request.method != "DELETE")
        {
            throw Error("invalid", "unsupported native web method");
        }
        if (request.timeout_seconds < 1 || request.timeout_seconds > 120 || request.max_bytes == 0 || request.max_bytes > 4 * 1024 * 1024 ||
            request.body.size() > MAX_REQUEST_BYTES)
        {
            throw Error("invalid", "native web request exceeds its configured limits");
        }
        if (request.digest_password.has_value() != request.digest_realm.has_value() ||
            (request.digest_realm && (request.digest_realm->empty() || request.digest_realm->size() > 256)))
        {
            throw Error("invalid", "native digest credentials require a pinned expected realm");
        }
        std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> client(curl_easy_init(), curl_easy_cleanup);
        if (!client)
        {
            throw Error("transport", "native web request could not be allocated");
        }
        ResponseBuffer buffer;
        buffer.maximum = request.max_bytes;
        buffer.digest_realm = request.digest_realm;
        option(curl_easy_setopt(client.get(), CURLOPT_URL, request.url.c_str()));
        option(curl_easy_setopt(client.get(), CURLOPT_CUSTOMREQUEST, request.method.c_str()));
        option(curl_easy_setopt(client.get(), CURLOPT_FOLLOWLOCATION, 0L));
        option(curl_easy_setopt(client.get(), CURLOPT_MAXREDIRS, 0L));
        option(curl_easy_setopt(client.get(), CURLOPT_SSL_VERIFYPEER, 1L));
        option(curl_easy_setopt(client.get(), CURLOPT_SSL_VERIFYHOST, 2L));
        option(curl_easy_setopt(client.get(), CURLOPT_PROXY, ""));
        option(curl_easy_setopt(client.get(), CURLOPT_NOSIGNAL, 1L));
        option(curl_easy_setopt(client.get(), CURLOPT_CONNECTTIMEOUT, static_cast<long>(request.timeout_seconds)));
        option(curl_easy_setopt(client.get(), CURLOPT_TIMEOUT, static_cast<long>(request.timeout_seconds)));
        option(curl_easy_setopt(client.get(), CURLOPT_USERAGENT, "switchboard-native/0.1"));
        option(curl_easy_setopt(client.get(), CURLOPT_WRITEFUNCTION, receive));
        option(curl_easy_setopt(client.get(), CURLOPT_WRITEDATA, &buffer));
        option(curl_easy_setopt(client.get(), CURLOPT_HEADERFUNCTION, receive_header));
        option(curl_easy_setopt(client.get(), CURLOPT_HEADERDATA, &buffer));
#if LIBCURL_VERSION_NUM >= 0x075500
        option(curl_easy_setopt(client.get(), CURLOPT_PROTOCOLS_STR, "http,https"));
#else
        option(curl_easy_setopt(client.get(), CURLOPT_PROTOCOLS, static_cast<long>(CURLPROTO_HTTP | CURLPROTO_HTTPS)));
#endif
        struct curl_slist *raw_headers = nullptr;
        for (const auto &[name, value] : request.headers)
        {
            if (name.empty() || name.size() > 128 || value.size() > 8192 ||
                !std::all_of(name.begin(), name.end(),
                             [](const unsigned char character)
                             {
                                 return std::isalnum(character) != 0 || character == '-';
                             }) ||
                value.find_first_of("\r\n") != std::string::npos || value.find('\0') != std::string::npos)
            {
                curl_slist_free_all(raw_headers);
                throw Error("invalid", "native web headers contain an invalid name or value");
            }
            auto *next = curl_slist_append(raw_headers, (name + ": " + value).c_str());
            if (next == nullptr)
            {
                curl_slist_free_all(raw_headers);
                throw Error("transport", "native web headers could not be allocated");
            }
            raw_headers = next;
        }
        const std::unique_ptr<curl_slist, decltype(&curl_slist_free_all)> headers(raw_headers, curl_slist_free_all);
        if (headers)
        {
            option(curl_easy_setopt(client.get(), CURLOPT_HTTPHEADER, headers.get()));
        }
        if (!request.body.empty() || request.method == "POST" || request.method == "PUT")
        {
            option(curl_easy_setopt(client.get(), CURLOPT_POSTFIELDS, request.body.data()));
            option(curl_easy_setopt(client.get(), CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(request.body.size())));
        }
        if (request.digest_password)
        {
            if (request.digest_password->size() > 8192 || request.digest_password->find('\0') != std::string::npos)
            {
                throw Error("invalid", "native digest credential is invalid");
            }
            option(curl_easy_setopt(client.get(), CURLOPT_HTTPAUTH, static_cast<long>(CURLAUTH_DIGEST)));
            option(curl_easy_setopt(client.get(), CURLOPT_USERNAME, "admin"));
            option(curl_easy_setopt(client.get(), CURLOPT_PASSWORD, request.digest_password->c_str()));
        }
        const auto result = curl_easy_perform(client.get());
        if (buffer.digest_rejected)
        {
            throw Error("authentication", "native provider changed or weakened its pinned digest challenge; credential resend was stopped");
        }
        if (buffer.exceeded)
        {
            throw Error("limit", "native web response exceeded its byte cap; write delivery may be unknown");
        }
        if (buffer.allocation_error || result != CURLE_OK)
        {
            throw Error("transport", "native web request failed; a submitted write must not be blindly repeated");
        }
        long status = 0;
        option(curl_easy_getinfo(client.get(), CURLINFO_RESPONSE_CODE, &status));
        if (request.digest_password && !buffer.digest_challenge_seen)
        {
            throw Error("authentication", "native provider omitted its required digest challenge; inspect any attempted effect before retrying");
        }
        return {status, std::move(buffer.headers), std::move(buffer.text)};
    }
};
}  // namespace

void validate_endpoint(const std::string &url, const bool allow_http)
{
    if (url.empty() || url.size() > 2048 || url.find('\0') != std::string::npos ||
        std::any_of(url.begin(), url.end(),
                    [](const unsigned char character)
                    {
                        return character <= 0x20 || character == 0x7F;
                    }))
    {
        throw Error("invalid", "native endpoints must be bounded addresses without control characters");
    }
    std::unique_ptr<CURLU, decltype(&curl_url_cleanup)> parsed(curl_url(), curl_url_cleanup);
    if (!parsed || curl_url_set(parsed.get(), CURLUPART_URL, url.c_str(), 0) != CURLUE_OK)
    {
        throw Error("invalid", "native endpoint is not a valid web address");
    }
    const auto scheme = part(parsed.get(), CURLUPART_SCHEME);
    if (scheme != "https" && !(scheme == "http" && allow_http))
    {
        throw Error("invalid", "native plain web transport requires explicit consent; other protocols are refused");
    }
    (void)part(parsed.get(), CURLUPART_HOST);
    if (!part(parsed.get(), CURLUPART_USER, true).empty() || !part(parsed.get(), CURLUPART_PASSWORD, true).empty() ||
        !part(parsed.get(), CURLUPART_QUERY, true).empty() || !part(parsed.get(), CURLUPART_FRAGMENT, true).empty() || url.find('?') != std::string::npos ||
        url.find('#') != std::string::npos || url.find('@') != std::string::npos)
    {
        throw Error("invalid", "native endpoints must not contain credentials, query strings or fragments");
    }
}

Json response_json(const HttpResponse &response)
{
    if (response.status < 200 || response.status >= 300)
    {
        throw Error("provider", "native provider returned an unsuccessful web status");
    }
    return parse_json(response.body);
}

std::unique_ptr<HttpTransport> make_http_transport()
{
    return std::make_unique<CurlTransport>();
}
}  // namespace switchboard

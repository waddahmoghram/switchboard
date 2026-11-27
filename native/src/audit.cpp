#include "audit_internal.hpp"

#include <algorithm>
#include <array>
#include <cstdint>
#include <utility>

#include <openssl/sha.h>

// Each value pattern of src/switchboard/audit.py is a scanner below that makes the same decisions as the regular
// expression, backtracking included, in one forward pass. The comment above each scanner quotes the expression.
// Classes are ASCII: \s is tab, line feed, vertical tab, form feed, carriage return, 0x1C-0x1F and space, \b uses
// [A-Za-z0-9_], and (?i) folds only A-Z. contracts/README.md lists where Python's Unicode classes differ.
namespace switchboard::audit
{
namespace
{
using Size = std::size_t;
constexpr Size NONE = std::string_view::npos;

bool is_upper(const char character)
{
    return character >= 'A' && character <= 'Z';
}

bool is_lower(const char character)
{
    return character >= 'a' && character <= 'z';
}

bool is_digit(const char character)
{
    return character >= '0' && character <= '9';
}

bool is_alpha(const char character)
{
    return is_upper(character) || is_lower(character);
}

bool is_alnum(const char character)
{
    return is_alpha(character) || is_digit(character);
}

bool is_word(const char character)
{
    return is_alnum(character) || character == '_';
}

bool is_base64url(const char character)
{
    return is_word(character) || character == '-';
}

bool is_hex(const char character)
{
    return is_digit(character) || (character >= 'a' && character <= 'f') || (character >= 'A' && character <= 'F');
}

bool is_space(const char character)
{
    const auto byte = static_cast<unsigned char>(character);
    return (byte >= 0x09 && byte <= 0x0d) || (byte >= 0x1c && byte <= 0x20);
}

bool is_blank(const char character)
{
    return character == ' ' || character == '\t';
}

bool is_quote(const char character)
{
    return character == '"' || character == '\'';
}

bool is_line_end(const char character)
{
    return character == '\r' || character == '\n';
}

bool is_lower_or_digit(const char character)
{
    return is_lower(character) || is_digit(character);
}

char fold(const char character)
{
    return is_upper(character) ? static_cast<char>(character - 'A' + 'a') : character;
}

template <typename Predicate> Size run_end(const std::string_view text, Size index, Predicate predicate)
{
    while (index < text.size() && predicate(text[index]))
    {
        ++index;
    }
    return index;
}

template <typename Predicate> bool has(const std::string_view text, const Size index, Predicate predicate)
{
    return index < text.size() && predicate(text[index]);
}

bool has_char(const std::string_view text, const Size index, const char expected)
{
    return index < text.size() && text[index] == expected;
}

// True when the character before index satisfies predicate, which makes a lookbehind (?<!class) fail.
template <typename Predicate> bool follows(const std::string_view text, const Size index, Predicate predicate)
{
    return index > 0 && predicate(text[index - 1]);
}

bool starts_with(const std::string_view text, const Size index, const std::string_view literal)
{
    return index <= text.size() && text.substr(index).starts_with(literal);
}

// Compares with a lower-case ASCII literal, folding only A-Z.
bool starts_with_folded(const std::string_view text, const Size index, const std::string_view literal)
{
    if (index > text.size() || text.size() - index < literal.size())
    {
        return false;
    }
    for (Size offset = 0; offset < literal.size(); ++offset)
    {
        if (fold(text[index + offset]) != literal[offset])
        {
            return false;
        }
    }
    return true;
}

bool equals_folded(const std::string_view text, const std::string_view literal)
{
    return text.size() == literal.size() && starts_with_folded(text, 0, literal);
}

bool contains_folded(const std::string_view text, const std::string_view literal)
{
    for (Size index = 0; index + literal.size() <= text.size(); ++index)
    {
        if (starts_with_folded(text, index, literal))
        {
            return true;
        }
    }
    return false;
}

std::string ascii_lower(std::string text)
{
    std::transform(text.begin(), text.end(), text.begin(), fold);
    return text;
}

// A match keeps the text from its start up to keep, then writes replacement and suffix in place of the rest up to
// end. A scanner returns the default (end NONE) when no match starts at the position.
struct Match
{
    Size end = NONE;
    Size keep = 0;
    std::string_view replacement;
    std::string_view suffix;
};

Match replaced(const Size start, const Size end, const std::string_view replacement)
{
    return {end, start, replacement, {}};
}

// A callback that returns the matched text unchanged still consumes it, so the search resumes after it.
Match unchanged(const Size end)
{
    return {end, end, {}, {}};
}

// re.sub: tries every start position from left to right and resumes at the end of each match; lookarounds see the
// text before this substitution. Returns false, leaving out untouched, when nothing matched.
template <typename Scanner> bool substitute(const std::string_view text, std::string &out, Scanner &&scanner)
{
    Size copied = 0;
    bool matched = false;
    for (Size start = 0; start < text.size();)
    {
        const auto match = scanner(text, start);
        if (match.end == NONE)
        {
            ++start;
            continue;
        }
        if (!matched)
        {
            out.clear();
            out.reserve(text.size() + 64);
            matched = true;
        }
        out.append(text.substr(copied, match.keep - copied));
        out.append(match.replacement);
        out.append(match.suffix);
        copied = start = match.end;
    }
    if (matched)
    {
        out.append(text.substr(copied));
    }
    return matched;
}

// Finds the first character at or after an index that satisfies stop. It remembers the stop-free stretch it last
// crossed, so the many nearby starts of one substitution do not rescan it.
class NextStop
{

public:
    explicit NextStop(bool (*stop)(char)) noexcept : stop_(stop)
    {
    }

    Size operator()(const std::string_view text, const Size from)
    {
        if (known_ && from >= from_ && from <= at_)
        {
            return at_;
        }
        auto index = from;
        while (index < text.size() && !stop_(text[index]))
        {
            if (known_ && index == from_)
            {
                index = at_;
                break;
            }
            ++index;
        }
        known_ = true;
        from_ = from;
        at_ = index;
        return index;
    }

private:
    bool (*stop_)(char);
    bool known_ = false;
    Size from_ = 0;
    Size at_ = 0;
};

bool is_label(const char character)
{
    return is_upper(character) || is_digit(character) || character == ' ';
}

// After -----BEGIN or -----END: [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?----- . The label holds no '-', so it is the whole
// run of label characters. Returns the end of the armor line.
Size key_armor(const std::string_view text, const Size index)
{
    const auto end = run_end(text, index, is_label);
    const auto label = text.substr(index, end - index);
    if ((!label.ends_with("PRIVATE KEY") && !label.ends_with("PRIVATE KEY BLOCK")) || !starts_with(text, end, "-----"))
    {
        return NONE;
    }
    return end + 5;
}

// (?s)-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)
Match private_key(const std::string_view text, const Size start)
{
    if (text[start] != '-' || !starts_with(text, start, "-----BEGIN"))
    {
        return {};
    }
    const auto header = key_armor(text, start + 10);
    if (header == NONE)
    {
        return {};
    }
    for (auto footer = text.find("-----END", header); footer != NONE; footer = text.find("-----END", footer + 1))
    {
        const auto end = key_armor(text, footer + 8);
        if (end != NONE)
        {
            return replaced(start, end, "[redacted:private-key]");
        }
    }
    return replaced(start, text.size(), "[redacted:private-key]");
}

// (?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*
Match jwt(const std::string_view text, const Size start)
{
    if (text[start] != 'e' || follows(text, start, is_base64url) || !starts_with(text, start, "eyJ"))
    {
        return {};
    }
    const auto header = run_end(text, start, is_base64url);
    if (header - start < 4 || !has_char(text, header, '.') || !starts_with(text, header + 1, "eyJ"))
    {
        return {};
    }
    const auto payload = run_end(text, header + 1, is_base64url);
    if (payload - (header + 1) < 4 || !has_char(text, payload, '.'))
    {
        return {};
    }
    return replaced(start, run_end(text, payload + 1, is_base64url), "[redacted:jwt]");
}

bool is_scheme(const char character)
{
    return is_alnum(character) || character == '+' || character == '.' || character == '-';
}

bool is_user(const char character)
{
    return !is_space(character) && character != ':' && character != '/' && character != '?' && character != '#' && character != '@';
}

bool is_password(const char character)
{
    return !is_space(character) && character != '/' && character != '?' && character != '#' && character != '@';
}

// (?i)\b([a-z][a-z0-9+.-]{0,30}://[^\s:/?#@]*):[^\s/?#@]+@ -> \1:[redacted]@
class Userinfo
{

public:
    Match operator()(const std::string_view text, const Size start)
    {
        if (!is_alpha(text[start]) || follows(text, start, is_word))
        {
            return {};
        }
        // The scheme class excludes ':', so "://" must follow the whole run, which may be at most 30 long.
        const auto scheme = run_end(text.substr(0, std::min(text.size(), start + 32)), start + 1, is_scheme);
        if (scheme - (start + 1) > 30 || !starts_with(text, scheme, "://") || scheme == failed_)
        {
            return {};
        }
        const auto user = run_end(text, scheme + 3, is_user);
        if (has_char(text, user, ':'))
        {
            const auto password = run_end(text, user + 1, is_password);
            if (password > user + 1 && has_char(text, password, '@'))
            {
                return {password + 1, user, ":[redacted]@", {}};
            }
        }
        // Every later start inside the same scheme reaches the same "://" and fails the same way.
        failed_ = scheme;
        return {};
    }

private:
    Size failed_ = NONE;
};

constexpr std::array<std::string_view, 19> QUERY_NAMES = {"access_token",
                                                          "refresh_token",
                                                          "id_token",
                                                          "token",
                                                          "api_key",
                                                          "apikey",
                                                          "key",
                                                          "secret",
                                                          "client_secret",
                                                          "password",
                                                          "passwd",
                                                          "auth",
                                                          "sig",
                                                          "signature",
                                                          "x-amz-signature",
                                                          "x-amz-credential",
                                                          "x-amz-security-token",
                                                          "x-goog-signature",
                                                          "x-goog-credential"};

bool is_query_value(const char character)
{
    return !is_space(character) && character != '&' && character != '#' && character != '"' && character != '\'' && character != '<' && character != '>';
}

// (?i)([?&;](?:access_token|refresh_token|...|x-goog-credential)=)[^&#\s\"'<>]+ -> \1[redacted]
// No name holds '=', so a name matches when it is the whole text up to the first '=' (at most 20 characters).
Match query(const std::string_view text, const Size start)
{
    if (text[start] != '?' && text[start] != '&' && text[start] != ';')
    {
        return {};
    }
    const auto equals = text.substr(0, std::min(text.size(), start + 22)).find('=', start + 1);
    if (equals == NONE)
    {
        return {};
    }
    const auto name = text.substr(start + 1, equals - start - 1);
    if (std::none_of(QUERY_NAMES.begin(), QUERY_NAMES.end(),
                     [name](const std::string_view candidate)
                     {
                         return equals_folded(name, candidate);
                     }))
    {
        return {};
    }
    const auto end = run_end(text, equals + 1, is_query_value);
    if (end == equals + 1)
    {
        return {};
    }
    return {end, equals + 1, "[redacted]", {}};
}

// password|passwd|passphrase|passcode|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token
// |auth[_-]?token|token|otp|pin : at most one alternative can match at a position. Returns the name end.
Size assignment_name(const std::string_view text, const Size start)
{
    static constexpr std::array<std::string_view, 8> SINGLE = {"password", "passwd", "passphrase", "passcode", "secret", "token", "otp", "pin"};
    static constexpr std::array<std::pair<std::string_view, std::string_view>, 5> JOINED = {
        {{"client", "secret"}, {"api", "key"}, {"access", "token"}, {"refresh", "token"}, {"auth", "token"}}};
    for (const auto name : SINGLE)
    {
        if (starts_with_folded(text, start, name))
        {
            return start + name.size();
        }
    }
    for (const auto &[head, tail] : JOINED)
    {
        if (starts_with_folded(text, start, head))
        {
            auto index = start + head.size();
            // The optional separator is greedy; without it the tail would have to start with '_' or '-'.
            if (has_char(text, index, '_') || has_char(text, index, '-'))
            {
                ++index;
            }
            return starts_with_folded(text, index, tail) ? index + tail.size() : NONE;
        }
    }
    return NONE;
}

bool is_assigned(const char character)
{
    return !is_space(character) && !is_quote(character) && character != ',' && character != ';' && character != '&' && character != '<' && character != '>';
}

bool ends_double_quoted(const char character)
{
    return character == '"' || is_line_end(character);
}

bool ends_single_quoted(const char character)
{
    return character == '\'' || is_line_end(character);
}

// (?i)(?<![a-z0-9])((?:names)[\"']?[ \t]*[:=][ \t]*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[\"']?[^\s\"',;&<>]+) -> _assigned
// A closed quoted value keeps its quotes around [redacted]; anything else becomes [redacted] after group 1.
class Assignment
{

public:
    Match operator()(const std::string_view text, const Size start)
    {
        if (!is_alpha(text[start]) || follows(text, start, is_alnum))
        {
            return {};
        }
        auto index = assignment_name(text, start);
        if (index == NONE)
        {
            return {};
        }
        if (has(text, index, is_quote))
        {
            ++index;
        }
        index = run_end(text, index, is_blank);
        if (!has_char(text, index, ':') && !has_char(text, index, '='))
        {
            return {};
        }
        const auto value = run_end(text, index + 1, is_blank);
        if (has(text, value, is_quote))
        {
            const auto close = text[value] == '"' ? double_quoted_(text, value + 1) : single_quoted_(text, value + 1);
            if (has_char(text, close, text[value]))
            {
                return {close + 1, value + 1, "[redacted]", text.substr(value, 1)};
            }
        }
        const auto bare = has(text, value, is_quote) ? value + 1 : value;
        const auto end = run_end(text, bare, is_assigned);
        if (end == bare)
        {
            return {};
        }
        return {end, value, "[redacted]", {}};
    }

private:
    NextStop double_quoted_{ends_double_quoted};
    NextStop single_quoted_{ends_single_quoted};
};

bool ends_cookie_pair(const char character)
{
    return is_space(character) || is_quote(character) || character == '=' || character == ';' || character == ',';
}

// [\"']?[ \t]*: after a header name; returns the colon.
Size header_colon(const std::string_view text, Size index)
{
    if (has(text, index, is_quote))
    {
        ++index;
    }
    index = run_end(text, index, is_blank);
    return has_char(text, index, ':') ? index : NONE;
}

// _HEADER_SCHEME, (?i)(?:basic|bearer|digest|negotiate|token)[ \t]+, at the start of the value [begin, end).
// Returns the end of the scheme, or begin without one.
Size scheme_end(const std::string_view text, const Size begin, const Size end)
{
    static constexpr std::array<std::string_view, 5> SCHEMES = {"basic", "bearer", "digest", "negotiate", "token"};
    const auto value = text.substr(begin, end - begin);
    for (const auto scheme : SCHEMES)
    {
        if (starts_with_folded(value, 0, scheme))
        {
            const auto blanks = run_end(value, scheme.size(), is_blank);
            return blanks > scheme.size() ? begin + blanks : begin;
        }
    }
    return begin;
}

// (?i)(?P<open>[\"'])?\b(?P<name>(?:(?:proxy-)?authorization|x-api-key|x-auth-token
// |(?:set-)?cookie(?=[\"']?[ \t]*:[ \t]*[\"']?[^\s=;,\"']+=))[\"']?[ \t]*:[ \t]*)
// (?:(?P<quote>[\"'])(?P<quoted>(?:\\.|(?!(?P=quote))[^\\\r\n])*)|(?P<bare>(?(open)(?:(?!(?P=open))[^\r\n])*|\S[^\r\n]*)))
// -> open, name, quote and scheme, then [redacted]; a closing quote stays after the match.
class Header
{

public:
    Match operator()(const std::string_view text, const Size start)
    {
        // A name cannot start at a quote, so a quote at the start is always the open group.
        const bool open = is_quote(text[start]);
        const auto name = open ? start + 1 : start;
        if (!has(text, name, is_alpha) || follows(text, name, is_word))
        {
            return {};
        }
        auto index = NONE;
        bool cookie = false;
        if (starts_with_folded(text, name, "proxy-authorization"))
        {
            index = name + 19;
        }
        else if (starts_with_folded(text, name, "authorization"))
        {
            index = name + 13;
        }
        else if (starts_with_folded(text, name, "x-api-key"))
        {
            index = name + 9;
        }
        else if (starts_with_folded(text, name, "x-auth-token"))
        {
            index = name + 12;
        }
        else if (starts_with_folded(text, name, "set-cookie"))
        {
            index = name + 10;
            cookie = true;
        }
        else if (starts_with_folded(text, name, "cookie"))
        {
            index = name + 6;
            cookie = true;
        }
        if (index == NONE)
        {
            return {};
        }
        const auto colon = header_colon(text, index);
        if (colon == NONE)
        {
            return {};
        }
        const auto value = run_end(text, colon + 1, is_blank);
        if (cookie)
        {
            // The lookahead repeats the separator, then needs [\"']?[^\s=;,\"']+= : a cookie name and '='.
            const auto pair = has(text, value, is_quote) ? value + 1 : value;
            const auto stop = cookie_pair_(text, pair);
            if (stop == pair || !has_char(text, stop, '='))
            {
                return {};
            }
        }
        if (has(text, value, is_quote))
        {
            const auto end = quoted_end(text, value + 1, text[value]);
            return {end, scheme_end(text, value + 1, end), "[redacted]", {}};
        }
        auto end = value;
        if (open)
        {
            while (end < text.size() && text[end] != text[start] && !is_line_end(text[end]))
            {
                ++end;
            }
        }
        else
        {
            if (value >= text.size() || is_space(text[value]))
            {
                return {};
            }
            while (end < text.size() && !is_line_end(text[end]))
            {
                ++end;
            }
        }
        return {end, scheme_end(text, value, end), "[redacted]", {}};
    }

private:
    // (?:\\.|(?!quote)[^\\\r\n])* : a backslash takes the next character unless it is a line feed.
    static Size quoted_end(const std::string_view text, Size index, const char quote)
    {
        while (index < text.size())
        {
            const auto character = text[index];
            if (character == '\\')
            {
                if (index + 1 < text.size() && text[index + 1] != '\n')
                {
                    index += 2;
                    continue;
                }
                break;
            }
            if (character == quote || is_line_end(character))
            {
                break;
            }
            ++index;
        }
        return index;
    }

    NextStop cookie_pair_{ends_cookie_pair};
};

bool is_bearer_token(const char character)
{
    return is_alnum(character) || character == '.' || character == '_' || character == '~' || character == '+' || character == '/' || character == '-';
}

bool is_padding(const char character)
{
    return character == '=';
}

// (?i)\b(bearer[ \t]+)([A-Za-z0-9._~+/-]{16,}=*) -> _bearer: redacted when the token, padding included, is at least
// 32 long or holds a digit; otherwise the match is kept as it is.
Match bearer(const std::string_view text, const Size start)
{
    if (fold(text[start]) != 'b' || follows(text, start, is_word) || !starts_with_folded(text, start, "bearer"))
    {
        return {};
    }
    const auto token = run_end(text, start + 6, is_blank);
    if (token == start + 6)
    {
        return {};
    }
    const auto run = run_end(text, token, is_bearer_token);
    if (run - token < 16)
    {
        return {};
    }
    const auto end = run_end(text, run, is_padding);
    if (end - token >= 32 || std::any_of(text.begin() + static_cast<std::ptrdiff_t>(token), text.begin() + static_cast<std::ptrdiff_t>(run), is_digit))
    {
        return {end, token, "[redacted]", {}};
    }
    return unchanged(end);
}

bool is_github_kind(const char character)
{
    return character == 'p' || character == 'o' || character == 'u' || character == 's' || character == 'r';
}

// (?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{22,})(?![A-Za-z0-9_])
Match github(const std::string_view text, const Size start)
{
    if (text[start] != 'g' || follows(text, start, is_word))
    {
        return {};
    }
    if (starts_with(text, start, "gh") && has(text, start + 2, is_github_kind) && has_char(text, start + 3, '_'))
    {
        const auto end = run_end(text, start + 4, is_alnum);
        return end - (start + 4) >= 20 && !has_char(text, end, '_') ? replaced(start, end, "[redacted:github-token]") : Match{};
    }
    if (starts_with(text, start, "github_pat_"))
    {
        const auto end = run_end(text, start + 11, is_word);
        return end - (start + 11) >= 22 ? replaced(start, end, "[redacted:github-token]") : Match{};
    }
    return {};
}

bool is_slack(const char character)
{
    return is_alnum(character) || character == '-';
}

bool is_slack_kind(const char character)
{
    return character == 'a' || character == 'b' || character == 'p' || character == 'r' || character == 's';
}

// (?<![A-Za-z0-9-])xox[abprs]-[A-Za-z0-9-]{10,}
Match slack(const std::string_view text, const Size start)
{
    if (text[start] != 'x' || follows(text, start, is_slack) || !starts_with(text, start, "xox") || !has(text, start + 3, is_slack_kind) ||
        !has_char(text, start + 4, '-'))
    {
        return {};
    }
    const auto end = run_end(text, start + 5, is_slack);
    return end - (start + 5) >= 10 ? replaced(start, end, "[redacted:slack-token]") : Match{};
}

bool is_aws(const char character)
{
    return is_upper(character) || is_digit(character);
}

// (?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])
Match aws(const std::string_view text, const Size start)
{
    if (text[start] != 'A' || follows(text, start, is_alnum) || (!starts_with(text, start, "AKIA") && !starts_with(text, start, "ASIA")))
    {
        return {};
    }
    const auto end = start + 20;
    if (end > text.size() || run_end(text.substr(0, end), start + 4, is_aws) != end || has(text, end, is_alnum))
    {
        return {};
    }
    return replaced(start, end, "[redacted:aws-key-id]");
}

// (?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{20,}|[rs]k_(?:live|test)_[A-Za-z0-9]{16,}|AIza[A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])
// -> _api_key: redacted only when the match holds a digit.
Match api_key(const std::string_view text, const Size start)
{
    const auto first = text[start];
    if ((first != 's' && first != 'r' && first != 'A') || follows(text, start, is_base64url))
    {
        return {};
    }
    auto end = NONE;
    if (starts_with(text, start, "sk-"))
    {
        const auto run = run_end(text, start + 3, is_base64url);
        end = run - (start + 3) >= 20 ? run : NONE;
    }
    else if (starts_with(text, start + 1, "k_live_") || starts_with(text, start + 1, "k_test_"))
    {
        const auto run = run_end(text, start + 8, is_alnum);
        end = run - (start + 8) >= 16 && !has_char(text, run, '_') && !has_char(text, run, '-') && first != 'A' ? run : NONE;
    }
    else if (starts_with(text, start, "AIza"))
    {
        const auto run = run_end(text, start + 4, is_base64url);
        end = run - (start + 4) == 35 ? run : NONE;
    }
    if (end == NONE)
    {
        return {};
    }
    if (std::any_of(text.begin() + static_cast<std::ptrdiff_t>(start), text.begin() + static_cast<std::ptrdiff_t>(end), is_digit))
    {
        return replaced(start, end, "[redacted:api-key]");
    }
    return unchanged(end);
}

// (?<![A-Za-z0-9])((?:bot)?[0-9]{6,12}):[A-Za-z0-9_-]{30,}(?![A-Za-z0-9_-]) -> \1:[redacted]
Match telegram(const std::string_view text, const Size start)
{
    if ((!is_digit(text[start]) && text[start] != 'b') || follows(text, start, is_alnum))
    {
        return {};
    }
    const auto digits = starts_with(text, start, "bot") ? start + 3 : start;
    const auto colon = run_end(text, digits, is_digit);
    if (colon - digits < 6 || colon - digits > 12 || !has_char(text, colon, ':'))
    {
        return {};
    }
    const auto end = run_end(text, colon + 1, is_base64url);
    return end - (colon + 1) >= 30 ? Match{end, colon, ":[redacted]", {}} : Match{};
}

// (?<![A-Za-z0-9])[0-9A-Fa-f]{32,}(?![A-Za-z0-9]) -> _hex: redacted when the run mixes digits and letters.
Match hex_run(const std::string_view text, const Size start)
{
    if (!is_hex(text[start]) || follows(text, start, is_alnum))
    {
        return {};
    }
    const auto end = run_end(text, start, is_hex);
    if (end - start < 32 || has(text, end, is_alnum))
    {
        return {};
    }
    const auto run = text.substr(start, end - start);
    if (std::any_of(run.begin(), run.end(), is_digit) && std::any_of(run.begin(), run.end(), is_alpha))
    {
        return replaced(start, end, "[redacted:hex]");
    }
    return unchanged(end);
}

// (?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9_-]) -> _base64url: redacted when the run mixes upper case, lower
// case and digits in _PIECE pieces, [A-Z]?[a-z]+|[A-Z]+(?![a-z])|[0-9]+, that average under three characters.
Match base64url_run(const std::string_view text, const Size start)
{
    if (!is_base64url(text[start]) || follows(text, start, is_base64url))
    {
        return {};
    }
    const auto end = run_end(text, start, is_base64url);
    if (end - start < 40)
    {
        return {};
    }
    Size pieces = 0;
    Size characters = 0;
    bool upper = false;
    bool lower = false;
    bool digit = false;
    for (auto index = start; index < end;)
    {
        auto next = index + 1;
        if (is_upper(text[index]))
        {
            upper = true;
            if (has(text, index + 1, is_lower))
            {
                lower = true;
                next = run_end(text, index + 1, is_lower);
            }
            else
            {
                // A capital run followed by lower case leaves its last capital to start the next piece.
                next = run_end(text, index, is_upper);
                if (has(text, next, is_lower))
                {
                    --next;
                }
            }
        }
        else if (is_lower(text[index]))
        {
            lower = true;
            next = run_end(text, index, is_lower);
        }
        else if (is_digit(text[index]))
        {
            digit = true;
            next = run_end(text, index, is_digit);
        }
        else
        {
            index = next;
            continue;
        }
        characters += next - index;
        ++pieces;
        index = next;
    }
    if (upper && lower && digit && characters < 3 * pieces)
    {
        return replaced(start, end, "[redacted:base64]");
    }
    return unchanged(end);
}

constexpr std::array<std::string_view, 13> SENSITIVE_PARTS = {"pass",    "secret",  "token",      "authorization", "authorisation", "cookie",     "apikey",
                                                              "api_key", "api-key", "credential", "privatekey",    "private_key",   "private-key"};
constexpr std::array<std::string_view, 11> SENSITIVE_SEGMENTS = {"otp", "totp", "hotp", "mfa", "2fa", "pin", "session", "private", "auth", "cvv", "cvc"};
constexpr std::array<std::string_view, 17> IDENTIFIER_SEGMENTS = {"id",      "ids",    "uid",      "uuid",     "hash",        "digest",
                                                                  "sha1",    "sha256", "sha512",   "checksum", "fingerprint", "head",
                                                                  "binding", "etag",   "revision", "action",   "target"};

template <std::size_t Count> bool listed(const std::string_view segment, const std::array<std::string_view, Count> &names)
{
    return std::any_of(names.begin(), names.end(),
                       [segment](const std::string_view name)
                       {
                           return equals_folded(segment, name);
                       });
}

// _KEY_SEGMENT, [A-Z]+[0-9]*(?![a-z])|[A-Z]?[a-z0-9]+, with its backtracking: "HTTPServer" is HTTP and Server, and
// "ABC12def" is ABC1 and 2def. Calls visit with each segment in order.
template <typename Visit> void key_segments(const std::string_view name, Visit visit)
{
    for (Size index = 0; index < name.size();)
    {
        auto end = index + 1;
        if (is_upper(name[index]))
        {
            const auto capitals = run_end(name, index, is_upper);
            const auto digits = run_end(name, capitals, is_digit);
            if (!has(name, digits, is_lower))
            {
                end = digits;
            }
            else if (digits > capitals)
            {
                end = digits - 1;
            }
            else if (capitals - index >= 2)
            {
                end = capitals - 1;
            }
            else
            {
                end = run_end(name, index + 1, is_lower_or_digit);
            }
        }
        else if (is_lower_or_digit(name[index]))
        {
            end = run_end(name, index, is_lower_or_digit);
        }
        else
        {
            index = end;
            continue;
        }
        visit(name.substr(index, end - index));
        index = end;
    }
}

struct KeyRule
{
    bool sensitive = false;
    bool identifier = false;
};

// A key is sensitive when it contains _SENSITIVE_KEY, pass(word)?|secret|token|authori[sz]ation|cookie|api[_-]?key
// |credential|private[_-]?key (case-insensitive), or has a segment in _SENSITIVE_SEGMENTS. Its value is an identifier
// when its last segment is in _IDENTIFIER_SEGMENTS.
KeyRule key_rule(const std::string_view name)
{
    KeyRule rule;
    rule.sensitive = std::any_of(SENSITIVE_PARTS.begin(), SENSITIVE_PARTS.end(),
                                 [name](const std::string_view part)
                                 {
                                     return contains_folded(name, part);
                                 });
    std::string_view last;
    bool segmented = false;
    key_segments(name,
                 [&](const std::string_view segment)
                 {
                     rule.sensitive = rule.sensitive || listed(segment, SENSITIVE_SEGMENTS);
                     last = segment;
                     segmented = true;
                 });
    rule.identifier = segmented && listed(last, IDENTIFIER_SEGMENTS);
    return rule;
}

bool hashed_name(const std::string_view name, const std::vector<std::string> &hash_only)
{
    return std::any_of(hash_only.begin(), hash_only.end(),
                       [name](const std::string &entry)
                       {
                           return equals_folded(name, entry);
                       });
}

// _digest: [{len} chars, sha256:{16 hex}], where len counts code points and the hash covers the UTF-8 text.
std::string digest(const std::string &text)
{
    static constexpr char DIGITS[] = "0123456789abcdef";
    unsigned char bytes[SHA256_DIGEST_LENGTH]{};
    if (SHA256(reinterpret_cast<const unsigned char *>(text.data()), text.size(), bytes) == nullptr)
    {
        throw Error("crypto", "native audit hashing failed");
    }
    const auto length = std::count_if(text.begin(), text.end(),
                                      [](const char character)
                                      {
                                          return (static_cast<unsigned char>(character) & 0xc0) != 0x80;
                                      });
    std::string result = "[";
    result += std::to_string(length);
    result += " chars, sha256:";
    for (Size index = 0; index < 8; ++index)
    {
        result.push_back(DIGITS[bytes[index] >> 4]);
        result.push_back(DIGITS[bytes[index] & 0x0f]);
    }
    result.push_back(']');
    return result;
}

Json redact_value(const Json &value, const Size depth, const std::vector<std::string> &hash_only, const bool identifier, const bool hashed)
{
    if (depth > MAX_DEPTH)
    {
        throw Error("limit", "native audit value exceeds its nesting limit");
    }
    if (value.is_object())
    {
        Json result = Json::object();
        for (auto item = value.begin(); item != value.end(); ++item)
        {
            const auto &name = item.key();
            const auto rule = key_rule(name);
            if (rule.sensitive)
            {
                result[name] = "[redacted]";
            }
            else
            {
                result[name] = redact_value(item.value(), depth + 1, hash_only, rule.identifier, hashed || hashed_name(name, hash_only));
            }
        }
        return result;
    }
    if (value.is_array())
    {
        Json result = Json::array();
        for (const auto &item : value)
        {
            result.push_back(redact_value(item, depth + 1, hash_only, identifier, hashed));
        }
        return result;
    }
    if (!value.is_string())
    {
        return value;
    }
    auto text = scrub(value.get_ref<const std::string &>(), identifier);
    return hashed ? Json(digest(text)) : Json(std::move(text));
}

// str.isspace(), which str.strip() removes.
bool python_space(const std::uint32_t code)
{
    return (code >= 0x09 && code <= 0x0d) || (code >= 0x1c && code <= 0x20) || code == 0x85 || code == 0xa0 || code == 0x1680 ||
           (code >= 0x2000 && code <= 0x200a) || code == 0x2028 || code == 0x2029 || code == 0x202f || code == 0x205f || code == 0x3000;
}

std::uint32_t code_point(const std::string_view text, const Size index)
{
    const auto lead = static_cast<unsigned char>(text[index]);
    const Size count = lead >= 0xf0 ? 3 : lead >= 0xe0 ? 2 : lead >= 0xc0 ? 1 : 0;
    std::uint32_t code = count == 0 ? lead : lead & (0x3fU >> count);
    for (Size offset = 1; offset <= count && index + offset < text.size(); ++offset)
    {
        code = (code << 6) | (static_cast<unsigned char>(text[index + offset]) & 0x3fU);
    }
    return code;
}

// name != name.strip() in Python: the first or last code point is whitespace.
bool padded(const std::string_view name)
{
    auto last = name.size() - 1;
    while (last > 0 && (static_cast<unsigned char>(name[last]) & 0xc0) == 0x80)
    {
        --last;
    }
    return python_space(code_point(name, 0)) || python_space(code_point(name, last));
}
}  // namespace

std::vector<std::string> hash_only_names(const Json &section)
{
    const auto found = section.find("hash_only");
    if (found == section.end())
    {
        return {};
    }
    std::vector<std::string> names;
    bool valid = found->is_array() && found->size() <= MAX_HASH_ONLY;
    for (auto item = found->begin(); valid && item != found->end(); ++item)
    {
        valid = item->is_string() && !item->get_ref<const std::string &>().empty() && !padded(item->get_ref<const std::string &>());
        if (valid)
        {
            names.push_back(ascii_lower(item->get<std::string>()));
        }
    }
    if (!valid)
    {
        throw Error("invalid", "native audit.hash_only must list up to 64 field names");
    }
    return names;
}

std::string scrub(const std::string_view text, const bool identifier)
{
    std::string current(text);
    std::string next;
    const auto apply = [&](auto &&scanner)
    {
        if (substitute(current, next, scanner))
        {
            current.swap(next);
        }
    };
    apply(private_key);
    apply(jwt);
    apply(Userinfo{});
    apply(query);
    apply(Assignment{});
    apply(Header{});
    apply(bearer);
    apply(github);
    apply(slack);
    apply(aws);
    apply(api_key);
    apply(telegram);
    if (!identifier)
    {
        apply(hex_run);
        apply(base64url_run);
    }
    return current;
}

Json redact(const Json &value, const std::vector<std::string> &hash_only)
{
    std::vector<std::string> names;
    names.reserve(hash_only.size());
    for (const auto &name : hash_only)
    {
        names.push_back(ascii_lower(name));
    }
    return redact_value(value, 0, names, false, false);
}
}  // namespace switchboard::audit

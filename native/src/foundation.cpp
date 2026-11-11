#include "switchboard/native.hpp"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <limits>
#include <regex>
#include <set>
#include <sstream>
#include <utility>

#include <dirent.h>
#include <fcntl.h>
#include <openssl/sha.h>
#include <sqlite3.h>
#include <sys/stat.h>
#include <unistd.h>

namespace switchboard
{
namespace
{
class File
{

public:
    explicit File(const int descriptor) : descriptor_(descriptor)
    {
        if (descriptor_ < 0)
        {
            throw Error("filesystem", "cannot open the configured private file or directory");
        }
    }

    ~File()
    {
        if (close(descriptor_) != 0)
        {
            std::fputs("ERROR: closing a native file descriptor failed\n", stderr);
        }
    }

    File(const File &) = delete;
    File &operator=(const File &) = delete;
    [[nodiscard]] int get() const noexcept
    {
        return descriptor_;
    }

private:
    int descriptor_;
};

struct Statement
{
    sqlite3_stmt *value = nullptr;

    ~Statement()
    {
        if (value != nullptr)
        {
            sqlite3_finalize(value);
        }
    }
};

void database_ok(const int result)
{
    if (result != SQLITE_OK)
    {
        throw Error("database", "native database operation failed; inspect permissions and available space");
    }
}

void prepare(sqlite3 *database, const std::string &sql, const std::vector<Json> &parameters, Statement &statement)
{
    if (sql.empty() || sql.size() > MAX_REQUEST_BYTES || sql.find('\0') != std::string::npos)
    {
        throw Error("database", "invalid native database statement");
    }
    const char *tail = nullptr;
    database_ok(sqlite3_prepare_v2(database, sql.c_str(), static_cast<int>(sql.size()), &statement.value, &tail));
    if (statement.value == nullptr)
    {
        throw Error("database", "native database statement is empty");
    }
    while (tail != nullptr && *tail != '\0')
    {
        if (std::isspace(static_cast<unsigned char>(*tail)) == 0)
        {
            throw Error("database", "only one native database statement is allowed");
        }
        ++tail;
    }
    if (sqlite3_bind_parameter_count(statement.value) != static_cast<int>(parameters.size()))
    {
        throw Error("database", "native database parameter count mismatch");
    }
    for (std::size_t index = 0; index < parameters.size(); ++index)
    {
        const auto &parameter = parameters[index];
        const int position = static_cast<int>(index + 1);
        int result = SQLITE_OK;
        if (parameter.is_null())
        {
            result = sqlite3_bind_null(statement.value, position);
        }
        else if (parameter.is_boolean())
        {
            result = sqlite3_bind_int(statement.value, position, parameter.get<bool>() ? 1 : 0);
        }
        else if (parameter.is_number_unsigned())
        {
            const auto number = parameter.get<std::uint64_t>();
            if (number > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
            {
                throw Error("database", "native integer parameter exceeds storage range");
            }
            result = sqlite3_bind_int64(statement.value, position, static_cast<std::int64_t>(number));
        }
        else if (parameter.is_number_integer())
        {
            result = sqlite3_bind_int64(statement.value, position, parameter.get<std::int64_t>());
        }
        else if (parameter.is_number_float() && std::isfinite(parameter.get<double>()))
        {
            result = sqlite3_bind_double(statement.value, position, parameter.get<double>());
        }
        else if (parameter.is_string())
        {
            const auto &text = parameter.get_ref<const std::string &>();
            if (text.size() > static_cast<std::size_t>(std::numeric_limits<int>::max()))
            {
                throw Error("database", "native text parameter is too large");
            }
            result = sqlite3_bind_text(statement.value, position, text.data(), static_cast<int>(text.size()), SQLITE_TRANSIENT);
        }
        else
        {
            throw Error("database", "unsupported native database parameter type");
        }
        database_ok(result);
    }
}

std::size_t text_length(const std::string &text)
{
    std::size_t count = 0;
    for (std::size_t index = 0; index < text.size();)
    {
        const auto first = static_cast<unsigned char>(text[index]);
        std::size_t width = 0;
        std::uint32_t value = 0;
        if (first < 0x80)
        {
            width = 1;
            value = first;
        }
        else if ((first & 0xE0) == 0xC0)
        {
            width = 2;
            value = first & 0x1F;
        }
        else if ((first & 0xF0) == 0xE0)
        {
            width = 3;
            value = first & 0x0F;
        }
        else if ((first & 0xF8) == 0xF0)
        {
            width = 4;
            value = first & 0x07;
        }
        else
        {
            throw Error("invalid", "native text must use valid UTF-8");
        }
        if (index + width > text.size())
        {
            throw Error("invalid", "native text must use complete UTF-8 characters");
        }
        for (std::size_t offset = 1; offset < width; ++offset)
        {
            const auto next = static_cast<unsigned char>(text[index + offset]);
            if ((next & 0xC0) != 0x80)
            {
                throw Error("invalid", "native text must use valid UTF-8");
            }
            value = (value << 6) | (next & 0x3F);
        }
        if ((width == 2 && value < 0x80) || (width == 3 && value < 0x800) || (width == 4 && value < 0x10000) || value > 0x10FFFF ||
            (value >= 0xD800 && value <= 0xDFFF))
        {
            throw Error("invalid", "native text contains an invalid Unicode scalar");
        }
        index += width;
        ++count;
    }
    return count;
}

void finite_json(const Json &value, const std::size_t depth = 0)
{
    if (depth > 64 || value.is_binary() || value.is_discarded())
    {
        throw Error("invalid", "native JSON exceeds the supported depth or value types");
    }
    if (value.is_number_float() && !std::isfinite(value.get<double>()))
    {
        throw Error("invalid", "native JSON numbers must be finite");
    }
    if (value.is_string())
    {
        text_length(value.get_ref<const std::string &>());
    }
    if (value.is_structured())
    {
        for (const auto &item : value)
        {
            finite_json(item, depth + 1);
        }
        if (value.is_object())
        {
            for (auto item = value.begin(); item != value.end(); ++item)
            {
                text_length(item.key());
            }
        }
    }
}

bool matches_type(const Json &value, const std::string &type)
{
    if (type == "object")
    {
        return value.is_object();
    }
    if (type == "array")
    {
        return value.is_array();
    }
    if (type == "string")
    {
        return value.is_string();
    }
    if (type == "integer")
    {
        return value.is_number_integer();
    }
    if (type == "number")
    {
        return value.is_number();
    }
    if (type == "boolean")
    {
        return value.is_boolean();
    }
    return type == "null" && value.is_null();
}

bool equal_json(const Json &left, const Json &right)
{
    if (left.is_boolean() || right.is_boolean())
    {
        return left.type() == right.type() && left == right;
    }
    if (left.is_object() && right.is_object())
    {
        if (left.size() != right.size())
        {
            return false;
        }
        for (auto item = left.begin(); item != left.end(); ++item)
        {
            if (!right.contains(item.key()) || !equal_json(item.value(), right.at(item.key())))
            {
                return false;
            }
        }
        return true;
    }
    if (left.is_array() && right.is_array())
    {
        if (left.size() != right.size())
        {
            return false;
        }
        for (std::size_t index = 0; index < left.size(); ++index)
        {
            if (!equal_json(left[index], right[index]))
            {
                return false;
            }
        }
        return true;
    }
    return left == right;
}

void schema_definition(const Json &schema, const std::size_t depth)
{
    if (!schema.is_object() || depth > 32)
    {
        throw Error("schema", "native schemas must be bounded objects");
    }
    const std::set<std::string> supported = {"type",          "enum",     "const",     "properties",    "required",      "additionalProperties",
                                             "propertyNames", "items",    "minLength", "maxLength",     "pattern",       "minimum",
                                             "maximum",       "minItems", "maxItems",  "minProperties", "maxProperties", "description",
                                             "title",         "default",  "examples",  "format",        "deprecated",    "readOnly",
                                             "writeOnly",     "$comment", "$schema"};
    for (auto item = schema.begin(); item != schema.end(); ++item)
    {
        if (!supported.contains(item.key()) && item.key().rfind("x-", 0) != 0)
        {
            throw Error("schema", "unsupported native schema keyword: " + item.key());
        }
    }
    if (schema.contains("type"))
    {
        const auto types = schema["type"].is_string() ? Json::array({schema["type"]}) : schema["type"];
        const std::set<std::string> names = {"object", "array", "string", "integer", "number", "boolean", "null"};
        if (!types.is_array() || types.empty())
        {
            throw Error("schema", "native schema type must be a supported name or nonempty array");
        }
        for (const auto &type : types)
        {
            if (!type.is_string() || !names.contains(type.get<std::string>()))
            {
                throw Error("schema", "unsupported native schema type");
            }
        }
    }
    for (const auto *key : {"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties"})
    {
        if (schema.contains(key) &&
            (!schema[key].is_number_integer() || schema[key].get<double>() < 0 || schema[key].get<double>() > static_cast<double>(MAX_JSON_INTEGER)))
        {
            throw Error("schema", "native schema size bounds must be nonnegative integers");
        }
    }
    for (const auto *key : {"minimum", "maximum"})
    {
        if (schema.contains(key) && (!schema[key].is_number() || !std::isfinite(schema[key].get<double>())))
        {
            throw Error("schema", "native schema numeric bounds must be finite");
        }
    }
    if (schema.contains("enum") && (!schema["enum"].is_array() || schema["enum"].empty()))
    {
        throw Error("schema", "native schema enum must be a nonempty array");
    }
    if (schema.contains("additionalProperties") && !schema["additionalProperties"].is_boolean())
    {
        throw Error("schema", "native schema additionalProperties must be boolean");
    }
    if (schema.contains("required"))
    {
        if (!schema["required"].is_array())
        {
            throw Error("schema", "native schema required must be an array");
        }
        for (const auto &name : schema["required"])
        {
            if (!name.is_string())
            {
                throw Error("schema", "native schema required entries must be names");
            }
        }
    }
    if (schema.contains("properties"))
    {
        if (!schema["properties"].is_object())
        {
            throw Error("schema", "native schema properties must be an object");
        }
        for (const auto &definition : schema["properties"])
        {
            schema_definition(definition, depth + 1);
        }
    }
    for (const auto *key : {"items", "propertyNames"})
    {
        if (schema.contains(key))
        {
            schema_definition(schema[key], depth + 1);
        }
    }
    if (schema.contains("pattern"))
    {
        if (!schema["pattern"].is_string() || schema["pattern"].get_ref<const std::string &>().size() > 1024)
        {
            throw Error("schema", "native schema patterns must be bounded strings");
        }
        try
        {
            const std::regex expression(schema["pattern"].get<std::string>());
            (void)expression;
        }
        catch (const std::regex_error &)
        {
            throw Error("schema", "invalid native schema pattern");
        }
    }
}

void validate_value(const Json &value, const Json &schema, const std::size_t depth)
{
    if (depth > 64)
    {
        throw Error("invalid", "native input exceeds its depth limit");
    }
    if (schema.contains("type"))
    {
        const auto types = schema["type"].is_string() ? Json::array({schema["type"]}) : schema["type"];
        if (std::none_of(types.begin(), types.end(),
                         [&](const Json &type)
                         {
                             return matches_type(value, type.get<std::string>());
                         }))
        {
            throw Error("invalid", "native input has the wrong type");
        }
    }
    if (schema.contains("const") && !equal_json(value, schema["const"]))
    {
        throw Error("invalid", "native input no longer matches the configured binding");
    }
    if (schema.contains("enum") && std::none_of(schema["enum"].begin(), schema["enum"].end(),
                                                [&](const Json &allowed)
                                                {
                                                    return equal_json(value, allowed);
                                                }))
    {
        throw Error("invalid", "native input is outside the configured choices");
    }
    if (value.is_object())
    {
        if ((schema.contains("minProperties") && value.size() < schema["minProperties"].get<std::size_t>()) ||
            (schema.contains("maxProperties") && value.size() > schema["maxProperties"].get<std::size_t>()))
        {
            throw Error("invalid", "native input has too many or too few properties");
        }
        for (const auto &key : schema.value("required", Json::array()))
        {
            if (!value.contains(key.get<std::string>()))
            {
                throw Error("invalid", "native input is missing a required field: " + key.get<std::string>());
            }
        }
        const auto properties = schema.value("properties", Json::object());
        for (auto item = value.begin(); item != value.end(); ++item)
        {
            if (schema.contains("propertyNames"))
            {
                validate_value(Json(item.key()), schema["propertyNames"], depth + 1);
            }
            if (properties.contains(item.key()))
            {
                validate_value(item.value(), properties[item.key()], depth + 1);
            }
            else if (!schema.value("additionalProperties", true))
            {
                throw Error("invalid", "native input has an unsupported field: " + item.key());
            }
        }
    }
    if (value.is_array())
    {
        if ((schema.contains("minItems") && value.size() < schema["minItems"].get<std::size_t>()) ||
            (schema.contains("maxItems") && value.size() > schema["maxItems"].get<std::size_t>()))
        {
            throw Error("invalid", "native input array is outside its size bounds");
        }
        if (schema.contains("items"))
        {
            for (const auto &item : value)
            {
                validate_value(item, schema["items"], depth + 1);
            }
        }
    }
    if (value.is_string())
    {
        const auto &text = value.get_ref<const std::string &>();
        const auto length = text_length(text);
        if ((schema.contains("minLength") && length < schema["minLength"].get<std::size_t>()) ||
            (schema.contains("maxLength") && length > schema["maxLength"].get<std::size_t>()))
        {
            throw Error("invalid", "native input text is outside its length bounds");
        }
        if (schema.contains("pattern") && !std::regex_search(text, std::regex(schema["pattern"].get<std::string>())))
        {
            throw Error("invalid", "native input text does not match the required pattern");
        }
    }
    if (value.is_number())
    {
        const auto number = value.get<double>();
        if ((schema.contains("minimum") && number < schema["minimum"].get<double>()) ||
            (schema.contains("maximum") && number > schema["maximum"].get<double>()))
        {
            throw Error("invalid", "native input number is outside its bounds");
        }
    }
}

void directory_info(const int descriptor)
{
    struct stat info
    {
    };
    if (fstat(descriptor, &info) != 0 || !S_ISDIR(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0777) != 0700)
    {
        throw Error("filesystem", "the native directory must be owned by you with mode 700");
    }
}
}  // namespace

Error::Error(std::string code, std::string message) : std::runtime_error(std::move(message)), code_(std::move(code))
{
}

const std::string &Error::code() const noexcept
{
    return code_;
}

class Database::Impl
{

public:
    sqlite3 *database = nullptr;

    ~Impl()
    {
        if (database != nullptr && sqlite3_close(database) != SQLITE_OK)
        {
            std::fputs("ERROR: closing the native database failed\n", stderr);
        }
    }
};

Database::Database(const std::filesystem::path &path) : impl_(std::make_unique<Impl>())
{
    File file(open(path.c_str(), O_CREAT | O_RDWR | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600));
    struct stat info
    {
    };
    if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 0077) != 0)
    {
        throw Error("database", "native state must be an owned private regular file");
    }
    database_ok(sqlite3_open_v2(path.c_str(), &impl_->database, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nullptr));
    database_ok(sqlite3_busy_timeout(impl_->database, 5000));
    execute("PRAGMA journal_mode = WAL");
    execute("PRAGMA foreign_keys = ON");
    Transaction transaction(*this);
    const auto versions = query("PRAGMA user_version");
    const auto version = versions.front()["user_version"].get<std::int64_t>();
    if (version != 0 && version != 1)
    {
        throw Error("database", "native state version is unsupported; do not reuse another backend's database");
    }
    if (version == 0 && !query("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").empty())
    {
        throw Error("database", "unversioned nonempty native state is refused rather than silently migrated");
    }
    if (version == 1)
    {
        const auto tables = query("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                                  "('native_approvals', 'native_calls', 'native_events', 'native_audit')");
        if (tables.size() != 4)
        {
            throw Error("database", "native state is incomplete; restore it rather than silently recreating missing history");
        }
    }
    execute("CREATE TABLE IF NOT EXISTS native_approvals (id TEXT PRIMARY KEY, tool TEXT NOT NULL, args TEXT NOT NULL, summary TEXT NOT NULL, "
            "status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, result TEXT, error TEXT)");
    execute(
        "CREATE TABLE IF NOT EXISTS native_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL)");
    execute("CREATE INDEX IF NOT EXISTS native_call_budget ON native_calls(tool, state, created_at)");
    execute("CREATE TABLE IF NOT EXISTS native_events (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, type TEXT NOT NULL, "
            "payload TEXT NOT NULL, dedupe_key TEXT UNIQUE, created_at INTEGER NOT NULL)");
    execute("CREATE TABLE IF NOT EXISTS native_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, previous_hash TEXT NOT NULL, record TEXT NOT NULL, hash TEXT NOT "
            "NULL)");
    execute("PRAGMA user_version = 1");
    transaction.commit();
}

Database::~Database() = default;

std::int64_t Database::execute(const std::string &sql, const std::vector<Json> &parameters)
{
    Statement statement;
    prepare(impl_->database, sql, parameters, statement);
    int result = SQLITE_ROW;
    while (result == SQLITE_ROW)
    {
        result = sqlite3_step(statement.value);
    }
    if (result != SQLITE_DONE)
    {
        throw Error("database", "native database write failed");
    }
    return sqlite3_changes(impl_->database);
}

std::vector<Json> Database::query(const std::string &sql, const std::vector<Json> &parameters)
{
    Statement statement;
    prepare(impl_->database, sql, parameters, statement);
    std::vector<Json> rows;
    int result = sqlite3_step(statement.value);
    while (result == SQLITE_ROW)
    {
        if (rows.size() >= 10000)
        {
            throw Error("limit", "native database result exceeds its row limit");
        }
        Json row = Json::object();
        for (int column = 0; column < sqlite3_column_count(statement.value); ++column)
        {
            const char *name = sqlite3_column_name(statement.value, column);
            switch (sqlite3_column_type(statement.value, column))
            {
            case SQLITE_INTEGER:
                row[name] = sqlite3_column_int64(statement.value, column);
                break;
            case SQLITE_FLOAT:
                row[name] = sqlite3_column_double(statement.value, column);
                break;
            case SQLITE_TEXT:
            {
                const auto *text = reinterpret_cast<const char *>(sqlite3_column_text(statement.value, column));
                const int bytes = sqlite3_column_bytes(statement.value, column);
                if (text == nullptr && bytes != 0)
                {
                    throw Error("database", "native stored text could not be decoded");
                }
                row[name] = bytes == 0 ? std::string() : std::string(text, static_cast<std::size_t>(bytes));
                break;
            }
            case SQLITE_NULL:
                row[name] = nullptr;
                break;
            default:
                throw Error("database", "native state contains an unsupported stored value");
            }
        }
        rows.push_back(std::move(row));
        result = sqlite3_step(statement.value);
    }
    if (result != SQLITE_DONE)
    {
        throw Error("database", "native database read failed");
    }
    return rows;
}

std::int64_t Database::last_insert_id() const
{
    return sqlite3_last_insert_rowid(impl_->database);
}

void Database::begin()
{
    execute("BEGIN IMMEDIATE");
}

void Database::commit()
{
    execute("COMMIT");
}

void Database::rollback() noexcept
{
    if (sqlite3_exec(impl_->database, "ROLLBACK", nullptr, nullptr, nullptr) != SQLITE_OK)
    {
        std::fputs("ERROR: native database rollback failed\n", stderr);
    }
}

Transaction::Transaction(Database &database) : database_(database)
{
    database_.begin();
}

Transaction::~Transaction()
{
    if (active_)
    {
        database_.rollback();
    }
}

void Transaction::commit()
{
    database_.commit();
    active_ = false;
}

Json parse_json(const std::string &text, const std::size_t maximum)
{
    if (text.size() > maximum)
    {
        throw Error("limit", "native JSON exceeds its byte limit");
    }
    std::vector<std::set<std::string>> keys;
    try
    {
        auto value = Json::parse(text,
                                 [&](const int depth, const Json::parse_event_t event, Json &parsed)
                                 {
                                     if (depth > 64)
                                     {
                                         throw Error("invalid", "native JSON exceeds its nesting limit");
                                     }
                                     if (event == Json::parse_event_t::object_start)
                                     {
                                         keys.emplace_back();
                                     }
                                     else if (event == Json::parse_event_t::key && !keys.back().insert(parsed.get<std::string>()).second)
                                     {
                                         throw Error("invalid", "duplicate native JSON object key");
                                     }
                                     else if (event == Json::parse_event_t::object_end)
                                     {
                                         keys.pop_back();
                                     }
                                     return true;
                                 });
        finite_json(value);
        return value;
    }
    catch (const Json::exception &)
    {
        throw Error("invalid", "native JSON is malformed or contains an unsupported number");
    }
}

std::string hash(const Json &value)
{
    finite_json(value);
    const auto text = value.dump(-1, ' ', true);
    unsigned char digest[SHA256_DIGEST_LENGTH]{};
    if (SHA256(reinterpret_cast<const unsigned char *>(text.data()), text.size(), digest) == nullptr)
    {
        throw Error("crypto", "native configuration hashing failed");
    }
    std::ostringstream result;
    result << std::hex << std::setfill('0');
    for (const auto byte : digest)
    {
        result << std::setw(2) << static_cast<unsigned int>(byte);
    }
    return result.str();
}

void check_schema(const Json &schema)
{
    finite_json(schema);
    schema_definition(schema, 0);
}

void validate(const Json &value, const Json &schema)
{
    check_schema(schema);
    finite_json(value);
    validate_value(value, schema, 0);
}

void check_object(const Json &value, const std::vector<std::string> &allowed, const std::vector<std::string> &required)
{
    if (!value.is_object())
    {
        throw Error("invalid", "expected a native configuration object");
    }
    for (auto item = value.begin(); item != value.end(); ++item)
    {
        if (std::find(allowed.begin(), allowed.end(), item.key()) == allowed.end())
        {
            throw Error("invalid", "unsupported native configuration field: " + item.key());
        }
    }
    for (const auto &key : required)
    {
        if (!value.contains(key))
        {
            throw Error("invalid", "missing native configuration field: " + key);
        }
    }
}

std::string string_field(const Json &value, const std::string &key, const std::size_t maximum, const bool required, const std::size_t minimum)
{
    if (!value.contains(key) && !required)
    {
        return {};
    }
    if (!value.contains(key) || !value[key].is_string())
    {
        throw Error("invalid", "native field must be text: " + key);
    }
    auto text = value[key].get<std::string>();
    const auto length = text_length(text);
    if (length < minimum || length > maximum || text.find('\0') != std::string::npos)
    {
        throw Error("invalid", "native text field is outside its bounds: " + key);
    }
    return text;
}

std::int64_t integer_field(const Json &value, const std::string &key, const std::int64_t minimum, const std::int64_t maximum, const std::int64_t fallback)
{
    if (!value.contains(key))
    {
        return fallback;
    }
    if (!value[key].is_number_integer() || value[key].get<double>() < static_cast<double>(minimum) || value[key].get<double>() > static_cast<double>(maximum))
    {
        throw Error("invalid", "native integer field is outside its bounds: " + key);
    }
    return value[key].get<std::int64_t>();
}

bool boolean_field(const Json &value, const std::string &key, const bool fallback)
{
    if (!value.contains(key))
    {
        return fallback;
    }
    if (!value[key].is_boolean())
    {
        throw Error("invalid", "native field must be boolean: " + key);
    }
    return value[key].get<bool>();
}

Json object_schema(const Json &properties, const std::vector<std::string> &required)
{
    return {{"type", "object"}, {"properties", properties}, {"required", required}, {"additionalProperties", false}};
}

std::string timestamp(const std::int64_t seconds)
{
    if (seconds < 0 || seconds > 253402300799LL)
    {
        throw Error("invalid", "native timestamp is outside the supported years");
    }
    const auto raw = static_cast<std::time_t>(seconds);
    std::tm calendar{};
    if (gmtime_r(&raw, &calendar) == nullptr)
    {
        throw Error("invalid", "native timestamp cannot be rendered");
    }
    std::ostringstream result;
    result << std::put_time(&calendar, "%Y-%m-%dT%H:%M:%SZ");
    return result.str();
}

std::int64_t parse_timestamp(const std::string &value)
{
    static const std::regex pattern(R"(^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})([Zz]|[+-][0-9]{2}:[0-9]{2})$)");
    std::smatch match;
    if (value.size() > 32 || !std::regex_match(value, match, pattern))
    {
        throw Error("invalid", "native dates require whole seconds and an explicit timezone offset");
    }
    const int year = std::stoi(match[1]);
    const unsigned int month = static_cast<unsigned int>(std::stoi(match[2]));
    const unsigned int day = static_cast<unsigned int>(std::stoi(match[3]));
    const int hour = std::stoi(match[4]);
    const int minute = std::stoi(match[5]);
    const int second = std::stoi(match[6]);
    const std::chrono::year_month_day date{std::chrono::year{year}, std::chrono::month{month}, std::chrono::day{day}};
    if (year < 1970 || !date.ok() || hour > 23 || minute > 59 || second > 59)
    {
        throw Error("invalid", "native date contains an invalid calendar or time component");
    }
    int offset = 0;
    const std::string zone = match[7];
    if (zone.size() > 1)
    {
        const int hours = std::stoi(zone.substr(1, 2));
        const int minutes = std::stoi(zone.substr(4, 2));
        if (hours > 23 || minutes > 59)
        {
            throw Error("invalid", "native date contains an invalid timezone offset");
        }
        offset = (hours * 3600 + minutes * 60) * (zone[0] == '+' ? 1 : -1);
    }
    const auto days = std::chrono::sys_days{date}.time_since_epoch();
    const auto result = std::chrono::duration_cast<std::chrono::seconds>(days).count() + hour * 3600 + minute * 60 + second - offset;
    if (result < 0 || result > 253402300799LL)
    {
        throw Error("invalid", "native date falls outside the supported instant range");
    }
    return result;
}

std::filesystem::path expand_path(const std::string &value)
{
    if (value.empty() || value.find('\0') != std::string::npos)
    {
        throw Error("invalid", "native paths must be nonempty");
    }
    std::string expanded = value;
    if (value == "~" || value.rfind("~/", 0) == 0)
    {
        const char *home = std::getenv("HOME");
        if (home == nullptr || *home != '/')
        {
            throw Error("filesystem", "an absolute HOME is required for native home-relative paths");
        }
        expanded = std::string(home) + value.substr(1);
    }
    const auto path = std::filesystem::path(expanded).lexically_normal();
    if (!path.is_absolute())
    {
        throw Error("invalid", "native paths must be absolute or start with ~/");
    }
    return path;
}

void ensure_private_directory(const std::filesystem::path &path)
{
    if (!path.is_absolute() || path == path.root_path())
    {
        throw Error("filesystem", "use a dedicated absolute native data directory");
    }
    struct stat info
    {
    };
    if (lstat(path.c_str(), &info) != 0)
    {
        if (errno != ENOENT || !std::filesystem::is_directory(path.parent_path()) || mkdir(path.c_str(), 0700) != 0)
        {
            throw Error("filesystem", "create the native directory's parent before use");
        }
    }
    File directory(open(path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
}

std::string read_private_file(const std::filesystem::path &root, const std::string &filename, const std::size_t maximum, const bool secret)
{
    if (filename.empty() || filename == "." || filename == ".." || filename.find_first_of("/\\") != std::string::npos ||
        filename.find('\0') != std::string::npos || maximum == 0 || maximum > 20 * 1024 * 1024)
    {
        throw Error("invalid", "choose a bounded native filename, not a path");
    }
    File directory(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(directory.get());
    File file(openat(directory.get(), filename.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    struct stat info
    {
    };
    if (fstat(file.get(), &info) != 0 || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & (secret ? 0077 : 0022)) != 0 ||
        info.st_size < 0 || static_cast<std::uint64_t>(info.st_size) > maximum)
    {
        throw Error("filesystem", "native input must be an owned, bounded regular file not writable by others");
    }
    std::string result;
    char buffer[8192];
    while (result.size() <= maximum)
    {
        const auto wanted = std::min(sizeof(buffer), maximum + 1 - result.size());
        const auto count = read(file.get(), buffer, wanted);
        if (count < 0)
        {
            if (errno == EINTR)
            {
                continue;
            }
            throw Error("filesystem", "native private input could not be read");
        }
        if (count == 0)
        {
            break;
        }
        result.append(buffer, static_cast<std::size_t>(count));
    }
    if (result.size() > maximum)
    {
        throw Error("limit", "native private input exceeds its actual byte limit");
    }
    return result;
}

std::vector<std::string> private_files(const std::filesystem::path &root, const std::size_t maximum)
{
    if (maximum == 0 || maximum > 10000)
    {
        throw Error("limit", "native directory entry limit is invalid");
    }
    File descriptor(open(root.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC));
    directory_info(descriptor.get());
    const int duplicate = dup(descriptor.get());
    if (duplicate < 0)
    {
        throw Error("filesystem", "native collection descriptor could not be duplicated");
    }
    DIR *raw = fdopendir(duplicate);
    if (raw == nullptr)
    {
        if (close(duplicate) != 0)
        {
            std::fputs("ERROR: closing a rejected native collection failed\n", stderr);
        }
        throw Error("filesystem", "native collection cannot be listed");
    }
    const auto close_directory = [](DIR *directory)
    {
        if (closedir(directory) != 0)
        {
            std::fputs("ERROR: closing a native collection failed\n", stderr);
        }
    };
    const std::unique_ptr<DIR, decltype(close_directory)> directory(raw, close_directory);
    std::vector<std::string> result;
    errno = 0;
    while (const auto *entry = readdir(directory.get()))
    {
        const std::string name = entry->d_name;
        if (name != "." && name != "..")
        {
            if (result.size() >= maximum)
            {
                throw Error("limit", "native collection exceeds its declared entry cap");
            }
            text_length(name);
            result.push_back(name);
        }
        errno = 0;
    }
    if (errno != 0)
    {
        throw Error("filesystem", "native collection listing failed");
    }
    std::sort(result.begin(), result.end());
    return result;
}
}  // namespace switchboard
